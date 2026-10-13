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
from src.market_calendar.service import MarketCalendar
from src.market_calendar.rules import candle_rule
from src.market_calendar.core import expected_bars
from src.market_calendar.intervals import exchange_date
from src.scheduler.timing import CandleScheduler

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
            # Метки дней брокера — даты по МСК: окно субботней дилерской сессии
            # с 23:00 UTC пятницы покрыто субботой, а не пятницей. Окно
            # известного дня — факт и тогда, когда лежит в соседних сутках.
            return (pd.Timestamp(exchange_date(point.to_pydatetime())) in self.covered_dates
                    or any(start <= point < end for start, end in self.intervals))
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

    def __init__(self, store: MarketDataStore, *, source: str, token=None, client_provider=None, refresh_seconds: int = 300, request_budget=None, market_calendar: MarketCalendar | None = None, calendar_evidence: bool = True, candle_publication_timeout_secs: float = 65, candle_publication_timeout_by_tf: dict[str, float] | None = None) -> None:
        self._store = store
        self._source = source
        self._token = token
        self._provider = client_provider
        self._refresh = pd.Timedelta(seconds=refresh_seconds)
        self._calendars: dict[str, tuple[pd.Timestamp, SessionCalendar | None]] = {}
        self._budget = request_budget
        self._market_calendar = market_calendar
        self._calendar_evidence = calendar_evidence
        self._publication_timeout = timedelta(seconds=candle_publication_timeout_secs)
        self._publication_timeout_by_tf = {
            timeframe: timedelta(seconds=seconds)
            for timeframe, seconds in (candle_publication_timeout_by_tf or {}).items()
        }
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
        if self._market_calendar is not None:
            self._reconcile_calendar(instrument_uid, interval, candles, now)
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

    def _reconcile_calendar(self, instrument_uid: str, interval: str, candles: pd.DataFrame, now: object) -> None:
        """Единый expected-sequence, включая хвост до последнего due бара."""
        current = to_naive(now).to_pydatetime()
        calendar = self._market_calendar.calendar_for(instrument_uid, current)
        rule = candle_rule(interval)
        grid = CandleScheduler(interval)
        stamps = sorted({to_naive(value).to_pydatetime() for value in candles["datetime"]})
        if not stamps:
            return
        ranges = [(grid.bar_close(left), right) for left, right in zip(stamps, stamps[1:])]
        timeout = self._publication_timeout_for(interval)
        expectation = self._market_calendar.expectation(instrument_uid, rule, current, timeout)
        if expectation.last_due is not None:
            ranges.append((grid.bar_close(stamps[-1]), expectation.last_due.close))

        for start, end in ranges:
            if end <= start:
                continue
            marker = (instrument_uid, interval, start, end, bool(calendar), self._calendar_evidence)
            if marker in self._scanned:
                continue
            self._scanned.add(marker)
            rows = []
            if calendar is None:
                rows = [(opened, "unresolved", "schedule_unavailable") for opened in self._grid_opens(grid, start, end)]
            else:
                due = {bar.open: bar for bar in expected_bars(calendar, rule, start, end, timeout)}
                for opened in self._grid_opens(grid, start, end):
                    closed = grid.bar_close(opened)
                    bar = due.get(opened)
                    if bar is not None:
                        rows.append((opened, "pending", self._schedule_evidence(calendar, bar.open, bar.close)))
                    elif self._calendar_covers(calendar, opened, closed):
                        if self._calendar_evidence and rule.rule_confirmed:
                            rows.append((opened, "scheduled_closed", self._schedule_evidence(calendar, opened, closed)))
                        else:
                            rows.append((opened, "unresolved", "calendar_evidence_disabled"))
                    else:
                        rows.append((opened, "unresolved", "schedule_unavailable"))
            self._store.set_interval_states(source=self._source, instrument_uid=instrument_uid, interval=interval,
                                            rows=rows, observed_at=now)

    @staticmethod
    def _grid_opens(grid, start, end):
        opened = grid.current_candle_start(start)
        if opened < start:
            opened = grid.bar_close(opened)
        while opened < end:
            yield opened
            opened = grid.bar_close(opened)

    @staticmethod
    def _calendar_covers(calendar, opened, closed) -> bool:
        return calendar.day(exchange_date(opened)) is not None or any(
            window.start < closed and opened < window.end
            for day in calendar.days for window in day.windows
        )

    @staticmethod
    def _schedule_evidence(calendar, opened, closed) -> str:
        day = next((day for day in calendar.days if any(
            window.start < closed and opened < window.end for window in day.windows
        )), None)
        if day is None:
            day = calendar.day(exchange_date(opened))
        return f"applicable_trading_schedule:{calendar.exchange}:{day.trading_date if day else exchange_date(opened)}:{day.version if day else 'none'}"

    def interval_state(self, *, instrument_uid: str, interval: str, open_time: object, now: object) -> str:
        """Возвращает долговечное либо вычисленное состояние минуты."""
        state = self._store.coverage_state(
            source=self._source, instrument_uid=instrument_uid, interval=interval, open_time=open_time,
        )
        if state in {"received", "scheduled_closed", "confirmed_no_trade"}:
            return state
        if self._market_calendar is not None and not self._calendar_evidence:
            return state or "unresolved"
        calendar = self._calendar_for(instrument_uid, now, at=open_time)
        if calendar is not None and calendar.covers(open_time) and not calendar.is_open(open_time):
            if state is not None:
                self._store.set_interval_state(source=self._source, instrument_uid=instrument_uid,
                    interval=interval, open_time=open_time, state="scheduled_closed",
                    evidence="applicable_trading_schedule", observed_at=now)
            return "scheduled_closed"
        return state or "unresolved"

    @property
    def market_calendar(self) -> MarketCalendar | None:
        return self._market_calendar

    def due_bar(self, *, instrument_uid: str, interval: str, now: object):
        """Кандидат публикационного срока из общего календаря либо ``None``."""
        if self._market_calendar is None:
            return None
        return self._market_calendar.expectation(
            instrument_uid, candle_rule(interval), to_naive(now).to_pydatetime(), self._publication_timeout_for(interval)
        ).last_due

    def calendar_known(self, *, instrument_uid: str, now: object) -> bool:
        return self._market_calendar is not None and self._market_calendar.calendar_for(
            instrument_uid, to_naive(now).to_pydatetime()
        ) is not None

    def calendar_expectation(self, *, instrument_uid: str, interval: str, now: object):
        if self._market_calendar is None:
            return None
        return self._market_calendar.expectation(
            instrument_uid, candle_rule(interval), to_naive(now).to_pydatetime(), self._publication_timeout_for(interval)
        )

    def expected_bars(self, *, instrument_uid: str, interval: str, lower: object, upper: object):
        if self._market_calendar is None:
            return ()
        upper_at = to_naive(upper).to_pydatetime()
        calendar = self._market_calendar.calendar_for(instrument_uid, upper_at)
        if calendar is None:
            return ()
        return expected_bars(calendar, candle_rule(interval), to_naive(lower).to_pydatetime(),
                             upper_at + timedelta(microseconds=1), self._publication_timeout_for(interval))

    def last_due_before(self, *, instrument_uid: str, interval: str, now: object):
        if self._market_calendar is None:
            return None
        return self._market_calendar.expectation(
            instrument_uid, candle_rule(interval), to_naive(now).to_pydatetime(), self._publication_timeout_for(interval)
        ).last_due

    def _publication_timeout_for(self, interval: str) -> timedelta:
        return self._publication_timeout_by_tf.get(interval, self._publication_timeout)

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
        if self._market_calendar is not None:
            calendar = self._market_calendar.calendar_for(instrument_uid, current.to_pydatetime())
            return _session_calendar_from_trading_calendar(calendar)
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
        if self._market_calendar is not None:
            return self._market_calendar.service(to_naive(now).to_pydatetime())
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


# Непрерывные торги, в которых формируются свечи. Аукционы и клиринг — перерыв;
# неизвестный тип считается торговым: ложная задержка лучше скрытого сбоя.
_BREAK_INTERVAL_MARKERS = ("auction", "clearing")


def _trading_interval(kind: str) -> bool:
    kind = (kind or "").lower()
    return not any(marker in kind for marker in _BREAK_INTERVAL_MARKERS)


def _minute_window(start, end):
    """Минуты, пересекающие окно брокера [start, end).

    Концы вида 05:59:59 и начала вида 16:00:01 захватывают свою минуту; конец
    ровно на минуте (20:50:00 FORTS, дальше клиринг) её не включает — так же
    окно трактует календарь торгов.
    """
    return to_naive(start).floor("min"), to_naive(end).ceil("min")


def _calendar_from_response(response: object, exchange: str, observed_at=None) -> SessionCalendar | None:
    schedules = getattr(response, "exchanges", ())
    # Код площадки в ответе приходит в верхнем регистре, в карточке — в нижнем.
    schedule = next((item for item in schedules
                     if str(getattr(item, "exchange", "")).lower() == str(exchange).lower()), None)
    if schedule is None:
        return None
    result: set[tuple[pd.Timestamp, pd.Timestamp]] = set()
    dates = []
    for day in getattr(schedule, "days", ()):
        day_date = getattr(day, "date", None)
        if day_date is not None:
            dates.append(to_naive(day_date).normalize())
        if not getattr(day, "is_trading_day", False):
            continue
        intervals = getattr(day, "intervals", ())
        for item in intervals:
            window = getattr(item, "interval", None)
            start, end = getattr(window, "start_ts", None), getattr(window, "end_ts", None)
            if start is not None and end is not None and _trading_interval(getattr(item, "type", "")):
                result.add(_minute_window(start, end))
        if not intervals:
            start, end = getattr(day, "start_time", None), getattr(day, "end_time", None)
            if start is not None and end is not None:
                lower, upper = _minute_window(start, end)
                clearing = (getattr(day, "clearing_start_time", None), getattr(day, "clearing_end_time", None))
                if all(value is not None and getattr(value, "year", 1970) > 1970 for value in clearing):
                    pause_start, pause_end = _minute_window(*clearing)
                    result.update(window for window in ((lower, pause_start), (pause_end, upper)) if window[0] < window[1])
                else:
                    result.add((lower, upper))
    if not dates and result:
        dates = sorted({stamp.normalize() for a, b in result for stamp in (a, b - pd.Timedelta(microseconds=1))})
    if not dates:
        return None
    return SessionCalendar(exchange, tuple(sorted(result)), min(dates), max(dates) + pd.Timedelta(days=1),
                           to_naive(observed_at) if observed_at is not None else None,
                           covered_dates=tuple(dates))


def _session_calendar_from_trading_calendar(calendar) -> SessionCalendar | None:
    """Временная проекция общей модели для старого API reconcilier.

    Сверка ещё использует ``SessionCalendar`` до задач раздела 4; загрузка и
    хранение расписания уже принадлежат ``MarketCalendar``.
    """
    if calendar is None:
        return None
    # Те же минутные окна, что у разбора ответа брокера (16:00:01 → минута 16:00).
    intervals = tuple(sorted({
        _minute_window(window.start, window.end)
        for day in calendar.days
        for window in day.windows
        if window.kind in ("continuous", "unknown")
    }))
    dates = tuple(pd.Timestamp(day.trading_date) for day in calendar.days)
    if not dates:
        return None
    return SessionCalendar(
        calendar.exchange,
        intervals,
        min(dates),
        max(dates) + pd.Timedelta(days=1),
        pd.Timestamp(min(day.observed_at for day in calendar.days)),
        calendar.source,
        dates,
    )


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
