"""Сверка последовательности свечей с расписанием и статусом брокера."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd

from src.api.retry import api_call_with_retry
from src.data.market_store import MarketDataStore
from src.data.timeutil import to_aware_utc, to_naive
from src.scheduler.timing import tf_period_minutes
from src.data.request_budget import RequestDeferred
from src.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SessionCalendar:
    """Снимок торговых интервалов одной биржи в UTC."""

    exchange: str
    intervals: tuple[tuple[pd.Timestamp, pd.Timestamp], ...]
    coverage_start: pd.Timestamp | None = None
    coverage_end: pd.Timestamp | None = None
    observed_at: pd.Timestamp | None = None
    source: str = "tbank_exchange"
    covered_dates: tuple[pd.Timestamp, ...] = field(default_factory=tuple)

    def covers(self, stamp: object) -> bool:
        point = to_naive(stamp)
        if self.covered_dates:
            return point.normalize() in self.covered_dates
        lower = self.coverage_start
        upper = self.coverage_end
        if lower is None or upper is None:
            if not self.intervals:
                return False
            lower = min(start for start, _ in self.intervals).normalize()
            upper = max(end for _, end in self.intervals).normalize() + pd.Timedelta(days=1)
        return lower <= point < upper

    def is_open(self, stamp: object) -> bool:
        point = to_naive(stamp)
        return self.covers(point) and any(start <= point < end for start, end in self.intervals)

    def next_open(self, stamp):
        return min((start for start, _ in self.intervals if start > to_naive(stamp)), default=None)


class MarketDataReconciler:
    """Знает, какие пропуски действительно следует ожидать от брокера.

    Отсутствие расписания трактуется консервативно: промежуток остаётся
    ``unresolved``. ``trading_status`` пишется для объяснения, но сам по себе
    не превращает пустой ответ в ``confirmed_no_trade``.
    """

    def __init__(self, store: MarketDataStore, *, source: str, token=None, client_provider=None, refresh_seconds: int = 300, request_budget=None) -> None:
        self._store = store
        self._source = source
        self._token = token
        self._provider = client_provider
        self._refresh = pd.Timedelta(seconds=refresh_seconds)
        self._calendars: dict[str, tuple[pd.Timestamp, SessionCalendar | None]] = {}
        self._budget = request_budget
        self._ranges: dict[str, list[SessionCalendar]] = {}
        self._calendar_tasks = {}
        self._calendar_retry = {}
        self._exchange = {}
        self.errors = {}
        self._scanned = set()
        self._recheck_after = {}

    def reconcile(self, *, instrument_uid: str, interval: str, candles: pd.DataFrame, now: object) -> None:
        """Помечает только пропуски *между* пришедшими свечами.

        Это ограничение не создаёт десятки тысяч строк ночного перерыва и не
        объявляет неявную минуту в конце полученного ряда потерянной раньше её
        нормальной публикации.
        """
        if candles is None or candles.empty or "datetime" not in candles:
            return
        calendar = self._calendar_for(instrument_uid, now)
        stamps = sorted({to_naive(value) for value in candles["datetime"]})
        step = pd.Timedelta(minutes=tf_period_minutes(interval))
        for left, right in zip(stamps, stamps[1:]):
            marker = (instrument_uid, interval, left, right)
            if marker in self._scanned:
                continue
            self._scanned.add(marker)
            probe = left + step
            while probe < right:
                applicable = calendar if calendar is not None and calendar.covers(probe) else self._calendar_for(instrument_uid, now, at=probe)
                if applicable is None or not applicable.covers(probe):
                    state, evidence = "unresolved", "schedule_unavailable"
                elif applicable.is_open(probe):
                    state, evidence = "pending", "open_session_missing_candle"
                else:
                    # Расписание сохранено наблюдением; отдельная строка
                    # планового перерыва не нужна, cache получает это из него.
                    probe += step
                    continue
                self._store.set_interval_state(
                    source=self._source, instrument_uid=instrument_uid,
                    interval=interval, open_time=probe, state=state, evidence=evidence,
                    observed_at=now,
                )
                probe += step

    def interval_state(self, *, instrument_uid: str, interval: str, open_time: object, now: object) -> str:
        """Возвращает долговечное либо вычисленное состояние минуты."""
        state = self._store.coverage_state(
            source=self._source, instrument_uid=instrument_uid, interval=interval, open_time=open_time,
        )
        if state in {"received", "scheduled_closed", "confirmed_no_trade"}:
            return state
        calendar = self._calendar_for(instrument_uid, now, at=open_time)
        if calendar is not None and calendar.covers(open_time) and not calendar.is_open(open_time):
            if state is not None:
                self._store.set_interval_state(source=self._source, instrument_uid=instrument_uid,
                    interval=interval, open_time=open_time, state="scheduled_closed",
                    evidence="applicable_trading_schedule", observed_at=now)
            return "scheduled_closed"
        return state or "unresolved"

    def recovery_candidate(self, *, instrument_uid: str, interval: str, now=None) -> pd.Timestamp | None:
        return self._store.first_recovery_candidate(
            source=self._source, instrument_uid=instrument_uid, interval=interval,
            now=now,
        )

    def record_empty_recovery(self, *, instrument_uid: str, interval: str, open_time: object) -> None:
        # Пустой ответ не означает отсутствия сделки. Сохраняем причину, чтобы
        # повторная попытка не стала молчаливым успехом.
        self._store.set_interval_state(
            source=self._source, instrument_uid=instrument_uid, interval=interval,
            open_time=open_time, state="unresolved", evidence="empty_recovery_response",
        )

    def _calendar_for(self, instrument_uid: str, now: object, at=None) -> SessionCalendar | None:
        current = to_naive(now)
        point = to_naive(at if at is not None else now)
        if self._budget is not None:
            if instrument_uid not in self._ranges:
                self._ranges[instrument_uid] = []
                for payload, observed in self._store.observations(source=self._source,
                        instrument_uid=instrument_uid, kind="trading_schedule"):
                    if "coverage_start" not in payload or "coverage_end" not in payload:
                        continue
                    self._ranges[instrument_uid].append(SessionCalendar(payload["exchange"],
                        tuple((to_naive(item["start"]), to_naive(item["end"])) for item in payload["intervals"]),
                        to_naive(payload["coverage_start"]), to_naive(payload["coverage_end"]), observed,
                        self._source, tuple(to_naive(day) for day in payload.get("covered_dates", ()))))
            found = next((cal for cal in reversed(self._ranges[instrument_uid]) if cal.covers(point)), None)
            if found is None or (point >= current.normalize() and current - found.observed_at >= self._refresh):
                if point.normalize() < current.normalize():
                    # TradingSchedules отклоняет `from` раньше текущей даты
                    # (INVALID_ARGUMENT 30003): прошлое доказывают только ранее
                    # сохранённые наблюдения, заведомо отклоняемый RPC не нужен.
                    return found
                lower = current.normalize()
                key = (instrument_uid, lower)
                if key not in self._calendar_tasks and current >= self._calendar_retry.get(key, pd.Timestamp.min):
                    self._calendar_tasks[key] = {"stage": 0 if instrument_uid not in self._exchange else 1,
                                                 "upper": lower + pd.Timedelta(days=8)}
            return found
        cached = self._calendars.get(instrument_uid)
        if cached is not None and current - cached[0] < self._refresh:
            return cached[1]
        calendar: SessionCalendar | None = None
        try:
            if self._provider is not None:
                context = self._provider.client_context(self._token)
            else:
                # Ленивый импорт не затягивает runtime-конфигурацию в unit
                # тесты чистой логики покрытия.
                from src.api.client import client_context
                context = client_context(self._token)
            with context as client:
                from t_tech.invest import InstrumentIdType

                instrument = api_call_with_retry(
                    client.instruments.get_instrument_by,
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                    id=instrument_uid,
                ).instrument
                status = api_call_with_retry(client.market_data.get_trading_status, instrument_id=instrument_uid)
                self._store.record_observation(
                    source=self._source, instrument_uid=instrument_uid, kind="trading_status",
                    payload=_jsonable(status), observed_at=current,
                )
                response = api_call_with_retry(
                    client.instruments.trading_schedules,
                    exchange=getattr(instrument, "exchange", ""),
                    from_=to_aware_utc(current).to_pydatetime(),
                    to=to_aware_utc(current + pd.Timedelta(days=7)).to_pydatetime(),
                )
                calendar = _calendar_from_response(response, getattr(instrument, "exchange", ""), observed_at=current)
                self._store.record_observation(
                    source=self._source, instrument_uid=instrument_uid, kind="trading_schedule",
                    payload={"exchange": calendar.exchange if calendar else getattr(instrument, "exchange", ""), "intervals": [
                        {"start": start.isoformat(), "end": end.isoformat()} for start, end in (calendar.intervals if calendar else ())
                    ]}, observed_at=current,
                )
        except Exception as exc:
            # Сбой наблюдения не позволяет принимать рискованное решение. Уже
            # известный календарь остаётся применимым до следующего успеха.
            calendar = cached[1] if cached is not None else None
            self.errors[instrument_uid] = str(exc)
            log.warning("Расписание инструмента %s не получено: %s", instrument_uid, exc)
        self._calendars[instrument_uid] = (current, calendar)
        return calendar

    def service_calendar(self, now):
        """Один RPC календаря из общего бюджета; выполняется вне lock кэша."""
        if self._budget is None or not self._calendar_tasks:
            return False
        current = to_naive(now)
        key = next(iter(self._calendar_tasks))
        uid, lower = key
        task = self._calendar_tasks[key]
        try:
            if self._provider is not None:
                context = self._provider.client_context(self._token)
            else:
                from src.api.client import client_context
                context = client_context(self._token)
            with context as client:
                if task["stage"] == 0:
                    from t_tech.invest import InstrumentIdType
                    response = self._budget.call(client.instruments.get_instrument_by,
                        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid, key=("instrument", uid))
                    self._exchange[uid] = response.instrument.exchange
                elif task["stage"] == 1:
                    response = self._budget.call(client.market_data.get_trading_status,
                        instrument_id=uid, key=("status", uid))
                    self._store.record_observation(source=self._source, instrument_uid=uid,
                        kind="trading_status", payload=_jsonable(response), observed_at=current)
                else:
                    response = self._budget.call(client.instruments.trading_schedules,
                        exchange=self._exchange[uid], from_=to_aware_utc(max(lower, current)).to_pydatetime(),
                        to=to_aware_utc(task["upper"]).to_pydatetime(), key=("schedule", key))
                    cal = _calendar_from_response(response, self._exchange[uid], observed_at=current)
                    if cal is None:
                        raise ValueError("В ответе нет применимого расписания")
                    ranges = self._ranges.setdefault(uid, [])
                    replaced = [old for old in ranges if old.covered_dates == cal.covered_dates]
                    self._ranges[uid] = [old for old in ranges if old.covered_dates != cal.covered_dates] + [cal]
                    if any(old.intervals != cal.intervals for old in replaced):
                        self._recheck_after = {k: v for k, v in self._recheck_after.items()
                                              if not (k[0] == uid and k[2:] == (cal.coverage_start, cal.coverage_end))}
                    self._store.record_observation(source=self._source, instrument_uid=uid,
                        kind="trading_schedule", observed_at=current, payload={
                            "exchange": cal.exchange, "coverage_start": cal.coverage_start.isoformat(),
                            "coverage_end": cal.coverage_end.isoformat(),
                            "covered_dates": [day.isoformat() for day in cal.covered_dates],
                            "intervals": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in cal.intervals]})
                    self.errors.pop(uid, None)
                    self._calendar_tasks.pop(key)
                    self._calendar_retry[key] = current + self._refresh
                    return True
                task["stage"] += 1
                # Другие UID получают RPC между фазами этой задачи.
                self._calendar_tasks.pop(key)
                self._calendar_tasks[key] = task
            return True
        except RequestDeferred:
            return False
        except Exception as exc:
            self.errors[uid] = str(exc)
            log.warning("Расписание %s в диапазоне %s — %s: %s", uid, lower, task["upper"], exc)
            self._calendar_tasks.pop(key)
            self._calendar_retry[key] = current + self._refresh
            return True

    def session_calendar(self, instrument_uid, now):
        return self._calendar_for(instrument_uid, now)

    def recheck(self, instrument_uid, interval, now):
        """Ограниченная переоценка 1000 старых статусов на новое доказательство."""
        for cal in self._ranges.get(instrument_uid, ()):
            key = (instrument_uid, interval, cal.coverage_start, cal.coverage_end)
            start = self._recheck_after.get(key, cal.coverage_start)
            stamps = self._store.unresolved_between(source=self._source, instrument_uid=instrument_uid,
                interval=interval, start=start, end=cal.coverage_end)
            for stamp in stamps:
                self.interval_state(instrument_uid=instrument_uid, interval=interval, open_time=stamp, now=now)
            self._recheck_after[key] = stamps[-1] + pd.Timedelta(microseconds=1) if len(stamps) == 1000 else cal.coverage_end


def _calendar_from_response(response: object, exchange: str, observed_at=None) -> SessionCalendar | None:
    schedules = getattr(response, "exchanges", ())
    schedule = next((item for item in schedules if getattr(item, "exchange", "") == exchange), None)
    if schedule is None:
        return None
    result: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    dates = []
    for day in getattr(schedule, "days", ()):
        day_date = getattr(day, "date", None)
        if day_date is not None:
            dates.append(to_naive(day_date).normalize())
        if not getattr(day, "is_trading_day", False):
            continue
        for item in getattr(day, "intervals", ()):
            window = getattr(item, "interval", None)
            start, end = getattr(window, "start_ts", None), getattr(window, "end_ts", None)
            if start is not None and end is not None:
                result.append((to_naive(start), to_naive(end)))
        if not getattr(day, "intervals", ()):
            start, end = getattr(day, "start_time", None), getattr(day, "end_time", None)
            if start is not None and end is not None:
                result.append((to_naive(start), to_naive(end)))
    if not dates and result:
        dates = sorted({stamp.normalize() for a, b in result for stamp in (a, b - pd.Timedelta(microseconds=1))})
    if not dates:
        return None
    return SessionCalendar(exchange, tuple(result), min(dates), max(dates) + pd.Timedelta(days=1),
                           to_naive(observed_at) if observed_at is not None else None,
                           covered_dates=tuple(dates))


def _jsonable(value: object) -> dict[str, Any]:
    annotations = getattr(value, "__annotations__", {})
    result: dict[str, Any] = {}
    for name in annotations:
        item = getattr(value, name, None)
        if hasattr(item, "name"):
            item = item.name
        elif isinstance(item, (pd.Timestamp,)):
            item = item.isoformat()
        elif hasattr(item, "isoformat"):
            item = item.isoformat()
        result[name] = item
    return result
