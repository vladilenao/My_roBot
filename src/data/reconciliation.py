"""Сверка последовательности свечей с расписанием и статусом брокера."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pandas as pd

from src.api.retry import api_call_with_retry
from src.data.market_store import MarketDataStore
from src.data.timeutil import to_aware_utc, to_naive
from src.scheduler.timing import tf_period_minutes


@dataclass(frozen=True)
class SessionCalendar:
    """Снимок торговых интервалов одной биржи в UTC."""

    exchange: str
    intervals: tuple[tuple[pd.Timestamp, pd.Timestamp], ...]

    def is_open(self, stamp: object) -> bool:
        point = to_naive(stamp)
        return any(start <= point < end for start, end in self.intervals)


class MarketDataReconciler:
    """Знает, какие пропуски действительно следует ожидать от брокера.

    Отсутствие расписания трактуется консервативно: промежуток остаётся
    ``unresolved``. ``trading_status`` пишется для объяснения, но сам по себе
    не превращает пустой ответ в ``confirmed_no_trade``.
    """

    def __init__(self, store: MarketDataStore, *, source: str, token=None, client_provider=None, refresh_seconds: int = 300) -> None:
        self._store = store
        self._source = source
        self._token = token
        self._provider = client_provider
        self._refresh = pd.Timedelta(seconds=refresh_seconds)
        self._calendars: dict[str, tuple[pd.Timestamp, SessionCalendar | None]] = {}

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
            probe = left + step
            while probe < right:
                if calendar is None:
                    state, evidence = "unresolved", "schedule_unavailable"
                elif calendar.is_open(probe):
                    state, evidence = "pending", "open_session_missing_candle"
                else:
                    # Расписание сохранено наблюдением; отдельная строка
                    # планового перерыва не нужна, cache получает это из него.
                    probe += step
                    continue
                self._store.set_interval_state(
                    source=self._source, instrument_uid=instrument_uid,
                    interval=interval, open_time=probe, state=state, evidence=evidence,
                )
                probe += step

    def interval_state(self, *, instrument_uid: str, interval: str, open_time: object, now: object) -> str:
        """Возвращает долговечное либо вычисленное состояние минуты."""
        state = self._store.coverage_state(
            source=self._source, instrument_uid=instrument_uid, interval=interval, open_time=open_time,
        )
        if state is not None:
            return state
        calendar = self._calendar_for(instrument_uid, now)
        return "scheduled_closed" if calendar is not None and not calendar.is_open(open_time) else "unresolved"

    def recovery_candidate(self, *, instrument_uid: str, interval: str) -> pd.Timestamp | None:
        return self._store.first_recovery_candidate(
            source=self._source, instrument_uid=instrument_uid, interval=interval,
        )

    def record_empty_recovery(self, *, instrument_uid: str, interval: str, open_time: object) -> None:
        # Пустой ответ не означает отсутствия сделки. Сохраняем причину, чтобы
        # повторная попытка не стала молчаливым успехом.
        self._store.set_interval_state(
            source=self._source, instrument_uid=instrument_uid, interval=interval,
            open_time=open_time, state="unresolved", evidence="empty_recovery_response",
        )

    def _calendar_for(self, instrument_uid: str, now: object) -> SessionCalendar | None:
        current = to_naive(now)
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
                    from_=to_aware_utc(current - pd.Timedelta(days=1)).to_pydatetime(),
                    to=to_aware_utc(current + pd.Timedelta(days=7)).to_pydatetime(),
                )
                calendar = _calendar_from_response(response, getattr(instrument, "exchange", ""))
                self._store.record_observation(
                    source=self._source, instrument_uid=instrument_uid, kind="trading_schedule",
                    payload={"exchange": calendar.exchange if calendar else getattr(instrument, "exchange", ""), "intervals": [
                        {"start": start.isoformat(), "end": end.isoformat()} for start, end in (calendar.intervals if calendar else ())
                    ]}, observed_at=current,
                )
        except Exception:
            # Сбой наблюдения не позволяет принимать рискованное решение. Уже
            # известный календарь остаётся применимым до следующего успеха.
            calendar = cached[1] if cached is not None else None
        self._calendars[instrument_uid] = (current, calendar)
        return calendar


def _calendar_from_response(response: object, exchange: str) -> SessionCalendar | None:
    schedules = getattr(response, "exchanges", ())
    schedule = next((item for item in schedules if getattr(item, "exchange", "") == exchange), None)
    if schedule is None:
        return None
    result: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for day in getattr(schedule, "days", ()):
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
    return SessionCalendar(exchange, tuple(result))


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
