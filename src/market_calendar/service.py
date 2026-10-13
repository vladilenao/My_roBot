"""Хранилище-ориентированный фасад календаря и broker-источник расписаний."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Protocol

from src.data.request_budget import RequestDeferred
from src.logging_setup import get_logger
from src.data.market_store import MarketDataStore, StoredScheduleDay

from .core import CalendarDay, CandleExpectation, TradingCalendar, expect
from .intervals import exchange_date, normalize_exchange, trading_date, windows_for_day
from .rules import CandleRule
from .serialization import canonical_day_json, day_version


log = get_logger(__name__)


def _utc_naive(value: object) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo is not None else stamp


def _aware_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


class CalendarSource(Protocol):
    source: str

    def request(self, instrument_uid: str, now: datetime, *, force: bool = False) -> None: ...

    def service(self, now: datetime) -> bool: ...

    def stale(self, exchange: str, now: datetime) -> bool: ...


class BrokerScheduleSource:
    """Запрашивает расписания T-Bank по режиму торгов, а не по UID."""

    source = "tbank"

    def __init__(
        self,
        store: MarketDataStore,
        provider,
        *,
        token=None,
        request_budget=None,
        horizon_days: int = 7,
        refresh_seconds: float = 3600,
    ) -> None:
        if horizon_days < 0:
            raise ValueError("horizon_days не может быть отрицательным")
        self._store = store
        self._provider = provider
        self._token = token
        self._budget = request_budget
        self._horizon_days = horizon_days
        self._refresh = timedelta(seconds=refresh_seconds)
        self._tasks: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._retry_after: dict[tuple[str, str], datetime] = {}
        self._failed_exchanges: dict[str, datetime] = {}
        self.errors: dict[str, str] = {}

    def request(self, instrument_uid: str, now: datetime, *, force: bool = False) -> None:
        """Регистрирует фоновую задачу без сетевого обращения."""
        now = _utc_naive(now)
        exchange = self._store.schedule_binding(source=self.source, instrument_uid=instrument_uid)
        if exchange is None:
            key = ("binding", instrument_uid)
        else:
            exchange = normalize_exchange(exchange)
            confirmed = self._store.schedule_last_confirmed_at(source=self.source, exchange=exchange)
            fresh = confirmed is not None and now - _utc_naive(confirmed) < self._refresh
            if fresh and not force:
                return
            key = ("schedule", exchange)
        if now >= self._retry_after.get(key, datetime.min):
            self._tasks.setdefault(key, None)

    def stale(self, exchange: str, now: datetime) -> bool:
        failed_at = self._failed_exchanges.get(exchange)
        return failed_at is not None and _utc_naive(now) - failed_at >= self._refresh

    def service(self, now: datetime) -> bool:
        """Выполняет не более одного RPC; отсрочка бюджета задачу не теряет."""
        if not self._tasks:
            return False
        now = _utc_naive(now)
        # Расписание режима сразу покрывает все его инструменты — раньше привязок.
        key = next((item for item in self._tasks if item[0] == "schedule"), next(iter(self._tasks)))
        kind, value = key
        try:
            with self._provider.client_context(self._token) as client:
                if kind == "binding":
                    from t_tech.invest import InstrumentIdType

                    response = self._call(
                        client.instruments.get_instrument_by,
                        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                        id=value,
                        key=("schedule_binding", value),
                    )
                    exchange = normalize_exchange(response.instrument.exchange)
                    if not exchange:
                        raise ValueError("Карточка инструмента не содержит exchange")
                    self._store.record_schedule_binding(
                        source=self.source, instrument_uid=value, exchange=exchange, observed_at=now
                    )
                    self._tasks.pop(key)
                    confirmed = self._store.schedule_last_confirmed_at(source=self.source, exchange=exchange)
                    if confirmed is None or now - _utc_naive(confirmed) >= self._refresh:
                        self._tasks.setdefault(("schedule", exchange), None)
                    self.errors.pop(value, None)
                    return True

                # `from` раньше текущей даты брокер отклоняет (30003, проба 8.5):
                # прошлое расписание недоступно, запрос — с текущего момента.
                today = exchange_date(now)
                upper = today + timedelta(days=self._horizon_days)
                response = self._call(
                    client.instruments.trading_schedules,
                    exchange=value,
                    from_=now.replace(tzinfo=timezone.utc),
                    to=_aware_day(upper),
                    key=("trading_schedules", value, today, upper),
                )
                schedule = next(
                    (item for item in getattr(response, "exchanges", ())
                     if normalize_exchange(getattr(item, "exchange", "")) == value),
                    None,
                )
                if schedule is None:
                    raise ValueError(f"В ответе нет расписания режима {value}")
                days = list(getattr(schedule, "days", ()))
                changed = sum(bool(self._store.record_schedule_day(source=self.source, exchange=value, day=day,
                                                                   observed_at=now)) for day in days)
                if changed or value in self._failed_exchanges:
                    log.info("Расписание %s: %d дн., новых версий %d", value, len(days), changed)
                self._tasks.pop(key)
                self._failed_exchanges.pop(value, None)
                self.errors.pop(value, None)
                return True
        except RequestDeferred:
            return False
        except Exception as exc:
            self._tasks.pop(key)
            # Повтор через короткую паузу, а не через час: пока расписания нет,
            # все минуты UNKNOWN. Ошибка видна в журнале.
            self._retry_after[key] = now + min(self._refresh, timedelta(seconds=60))
            self.errors[value] = str(exc)
            log.warning("Расписание торгов (%s %s) не получено: %s", kind, value, exc)
            if kind == "schedule":
                self._failed_exchanges[value] = now
            return True

    def _call(self, fn, *args, key, **kwargs):
        if self._budget is None:
            return fn(*args, **kwargs)
        # Окно темпа свечных дозагрузок не занимается: запросов календаря мало
        # (привязка — раз на UID, расписание — раз в час на режим), а квоты
        # сервиса инструментов, поминутный потолок и retry-after действуют.
        return self._budget.call(fn, *args, key=key, service="instruments", pace=False, **kwargs)


class EmulatorScheduleSource:
    """Расписание исторического снимка без обращения к T-Bank."""

    source = "emulator"

    def __init__(self, store: MarketDataStore, provider, *, horizon_days: int = 7) -> None:
        self._store = store
        self._provider = provider
        self._horizon_days = horizon_days
        self._tasks: OrderedDict[tuple[str, date], None] = OrderedDict()
        self._loaded: dict[str, list[tuple[date, date]]] = {}
        self._failed: dict[str, list[tuple[date, date]]] = {}
        self.errors: dict[str, str] = {}
        # Для отчёта прогона: какие даты понадобились и каких нет в снимке.
        self.requested: dict[str, set[date]] = {}
        self.missing_dates: set[date] = set()

    def request(self, instrument_uid: str, now: datetime, *, force: bool = False) -> None:
        # Снимок неизменяем: уже проверенный диапазон, в том числе пустой после
        # 404, не надо повторно запрашивать на каждом историческом тике.
        requested_date = exchange_date(_utc_naive(now))
        self.requested.setdefault(instrument_uid, set()).add(requested_date)
        if self._contains(self._loaded.get(instrument_uid, ()), requested_date):
            return
        if self._contains(self._failed.get(instrument_uid, ()), requested_date):
            return
        self._tasks.setdefault((instrument_uid, requested_date), None)

    def stale(self, exchange: str, now: datetime) -> bool:
        return False

    @property
    def snapshot_generation(self) -> int | None:
        return getattr(self._provider, "snapshot_generation", None)

    def service(self, now: datetime) -> bool:
        if not self._tasks:
            return False
        now = _utc_naive(now)
        instrument_uid, requested_date = next(iter(self._tasks))
        lower = requested_date - timedelta(days=self._horizon_days)
        upper = requested_date + timedelta(days=self._horizon_days)
        try:
            with self._provider.client_context() as client:
                response = client.trading_schedules(
                    instrument_id=instrument_uid,
                    from_=_aware_day(lower),
                    to=_aware_day(upper),
                )
            if not isinstance(response, dict):
                raise ValueError("Эмулятор вернул некорректное расписание")
            response_uid = response.get("instrument_id")
            if response_uid is not None and response_uid != instrument_uid:
                raise ValueError("Эмулятор вернул расписание другого инструмента")
            for value in response.get("missing_dates", ()) or ():
                try:
                    self.missing_dates.add(date.fromisoformat(str(value)))
                except ValueError:
                    continue
            rows = response.get("days", ())
            if not isinstance(rows, (list, tuple)) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Эмулятор вернул некорректные дни расписания")
            if not rows:
                raise ValueError("Эмулятор не вернул расписание инструмента")
            exchange = normalize_exchange(rows[0].get("exchange"))
            if not exchange or any(normalize_exchange(row.get("exchange")) != exchange for row in rows):
                raise ValueError("Эмулятор вернул дни разных или пустых режимов торгов")
            self._store.record_schedule_binding(
                source=self.source, instrument_uid=instrument_uid, exchange=exchange, observed_at=now
            )
            for row in rows:
                day = _sdk_day(row.get("day"), row.get("trading_date"))
                claimed_version = row.get("version")
                if claimed_version and claimed_version != day_version(day):
                    raise ValueError("Версия дня эмулятора не совпадает с каноническим содержимым")
                self._store.record_schedule_day(source=self.source, exchange=exchange, day=day, observed_at=now)
            self._tasks.pop((instrument_uid, requested_date))
            self._loaded.setdefault(instrument_uid, []).append((lower, upper))
            self.errors.pop(instrument_uid, None)
            return True
        except Exception as exc:
            self._tasks.pop((instrument_uid, requested_date))
            if getattr(exc, "code", None) == "SNAPSHOT_MISMATCH":
                raise
            self._failed.setdefault(instrument_uid, []).append((lower, upper))
            if getattr(exc, "status", None) == 404:
                self.errors[instrument_uid] = "schedule_resource_unavailable"
            else:
                self.errors[instrument_uid] = f"schedule_unavailable: {exc}"
            return True

    @staticmethod
    def _contains(ranges, requested_date: date) -> bool:
        return any(lower <= requested_date <= upper for lower, upper in ranges)


class SnapshotScheduleStore:
    """Непостоянное хранилище расписания одного historical snapshot."""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], str] = {}
        self._days: dict[tuple[str, str, str], StoredScheduleDay] = {}
        self._change_id = 0

    def record_schedule_binding(self, *, source, instrument_uid, exchange, observed_at=None):
        exchange = normalize_exchange(exchange)
        key = (source, instrument_uid)
        changed = self._bindings.get(key) != exchange
        self._bindings[key] = exchange
        if changed:
            self._change_id += 1
        return changed

    def schedule_binding(self, *, source, instrument_uid):
        return self._bindings.get((source, instrument_uid))

    def record_schedule_day(self, *, source, exchange, day, observed_at=None):
        exchange = normalize_exchange(exchange)
        version = day_version(day)
        key = (source, exchange, trading_date(day).isoformat())
        previous = self._days.get(key)
        when = _utc_naive(observed_at or datetime.now(timezone.utc)).isoformat() + "Z"
        if previous is not None and previous.version == version:
            self._days[key] = StoredScheduleDay(**{**previous.__dict__, "last_confirmed_at": when})
            return False
        self._change_id += 1
        self._days[key] = StoredScheduleDay(source, exchange, key[2], version, canonical_day_json(day),
            bool(getattr(day, "is_trading_day", False)), when, when, self._change_id)
        return True

    def schedule_days(self, *, source, exchange):
        exchange = normalize_exchange(exchange)
        return tuple(sorted((day for (item_source, item_exchange, _), day in self._days.items()
                             if item_source == source and item_exchange == exchange), key=lambda day: day.trading_date))

    def schedule_last_confirmed_at(self, *, source, exchange):
        days = self.schedule_days(source=source, exchange=exchange)
        return max((day.last_confirmed_at for day in days), default=None)

class MarketCalendar:
    """Read-only на тике календарный фасад поверх сохранённого расписания."""

    def __init__(
        self,
        store: MarketDataStore,
        source: CalendarSource,
        *,
        schedule_max_age_secs: float = 129600,
    ) -> None:
        if schedule_max_age_secs <= 0:
            raise ValueError("schedule_max_age_secs должен быть положительным")
        self._store = store
        self._source = source
        self._max_age = timedelta(seconds=schedule_max_age_secs)
        # Ожидание запрашивается каждой парой на каждом пробуждении: SQLite и
        # разбор JSON — только после записи фоновым обслуживанием.
        self._lock = threading.Lock()
        self._bindings: dict[str, str] = {}
        self._records: dict[str, tuple[StoredScheduleDay, ...]] = {}
        self._parsed: dict[tuple[str, str, str], CalendarDay] = {}
        self._used: set[tuple[str, str, str]] = set()

    def expectation(
        self,
        instrument_uid: str,
        rule: CandleRule,
        now: datetime,
        timeout: timedelta,
    ) -> CandleExpectation:
        now = _utc_naive(now)
        calendar = self.calendar_for(instrument_uid, now)
        result = expect(calendar, rule, now, timeout, instrument_uid=instrument_uid)
        if result.schedule is not None:
            used = {(result.schedule.exchange, day.isoformat(), version) for day, version in result.schedule.days}
            if not used <= self._used:
                with self._lock:
                    self._used |= used
        reason = getattr(self._source, "errors", {}).get(instrument_uid)
        if reason and result.phase.value == "unknown":
            return CandleExpectation(
                result.instrument_uid, result.timeframe, result.now, result.phase,
                result.break_kind, result.last_due, result.next_event, result.schedule, reason,
            )
        return result

    def calendar_for(self, instrument_uid: str, now: datetime) -> TradingCalendar | None:
        """Возвращает только сохранённый календарь и при необходимости ставит загрузку."""
        now = _utc_naive(now)
        exchange = self._binding(instrument_uid)
        if exchange is None:
            self._source.request(instrument_uid, now, force=True)
            return None
        calendar = self.calendar(exchange, now)
        self._source.request(instrument_uid, now, force=calendar is None or calendar.day(exchange_date(now)) is None)
        return calendar

    def exchange_for(self, instrument_uid: str) -> str | None:
        """Сохранённый режим торгов UID без сетевого обращения."""
        return self._binding(instrument_uid)

    def calendar(self, exchange: str, now: datetime) -> TradingCalendar | None:
        now = _utc_naive(now)
        exchange = normalize_exchange(exchange)
        days = [day for day in self._days(exchange) if self._applicable(day)]
        if not days:
            return None
        return TradingCalendar(
            source=self._source.source,
            exchange=exchange,
            days=tuple(self._parse(day) for day in days),
            stale=self._source.stale(exchange, now),
            snapshot_generation=getattr(self._source, "snapshot_generation", None),
        )

    def service(self, now: datetime) -> bool:
        worked = self._source.service(_utc_naive(now))
        if worked:
            with self._lock:
                self._bindings.clear()
                self._records.clear()
        return worked

    def usage(self) -> dict:
        """Использованное расписание для отчёта прогона: режимы, версии, пробелы."""
        source = self._source
        with self._lock:
            versions = set(self._used)
        exchanges = {exchange for exchange, _, _ in versions}
        missing: set[date] = set(getattr(source, "missing_dates", ()))
        for uid, dates in getattr(source, "requested", {}).items():
            exchange = self._binding(uid)
            if exchange is None:
                missing.update(dates)
                continue
            exchanges.add(exchange)
            known = {record.trading_date for record in self._days(exchange)}
            missing.update(value for value in dates if value.isoformat() not in known)
        reasons = sorted(set(getattr(source, "errors", {}).values()))
        generation = getattr(source, "snapshot_generation", None)
        return {
            "source": source.source,
            "snapshot_generation": generation,
            "snapshot_pinned": generation is not None,
            "exchanges": tuple(sorted(exchanges)),
            "day_versions": tuple(sorted(versions)),
            "missing_dates": tuple(value.isoformat() for value in sorted(missing)),
            "unavailable_reason": "; ".join(reasons) or None,
        }

    def _binding(self, instrument_uid: str) -> str | None:
        with self._lock:
            cached = self._bindings.get(instrument_uid)
        if cached is not None:
            return cached
        exchange = self._store.schedule_binding(source=self._source.source, instrument_uid=instrument_uid)
        if exchange is None:
            return None
        exchange = normalize_exchange(exchange)
        with self._lock:
            self._bindings[instrument_uid] = exchange
        return exchange

    def _days(self, exchange: str) -> tuple[StoredScheduleDay, ...]:
        with self._lock:
            cached = self._records.get(exchange)
        if cached is not None:
            return cached
        records = tuple(self._store.schedule_days(source=self._source.source, exchange=exchange))
        if records:
            with self._lock:
                self._records[exchange] = records
        return records

    def _parse(self, record: StoredScheduleDay) -> CalendarDay:
        key = (record.exchange, record.trading_date, record.version)
        with self._lock:
            parsed = self._parsed.get(key)
        if parsed is None:
            parsed = _calendar_day(record)
            with self._lock:
                self._parsed[key] = parsed
        # Время наблюдения версии могло обновиться (повтор без изменений).
        return parsed if parsed.observed_at == _utc_naive(record.observed_at) else replace(
            parsed, observed_at=_utc_naive(record.observed_at))

    def _applicable(self, day: StoredScheduleDay) -> bool:
        if self._source.source == "emulator":
            return True
        start = datetime.combine(date.fromisoformat(day.trading_date), time.min)
        return _utc_naive(day.last_confirmed_at) >= start - self._max_age


def _calendar_day(record: StoredScheduleDay) -> CalendarDay:
    raw = json.loads(record.raw_json)
    values = {name: _decode(value) for name, value in raw.items() if name != "intervals"}
    intervals = [
        SimpleNamespace(
            type=item.get("type", ""),
            interval=SimpleNamespace(
                start_ts=_decode(item.get("interval", {}).get("start_ts")),
                end_ts=_decode(item.get("interval", {}).get("end_ts")),
            ),
        )
        for item in raw.get("intervals", ())
    ]
    day = SimpleNamespace(**values, intervals=intervals)
    return CalendarDay(
        trading_date=date.fromisoformat(record.trading_date),
        is_trading_day=record.is_trading_day,
        windows=windows_for_day(day),
        version=record.version,
        observed_at=_utc_naive(record.observed_at),
    )


def _decode(value):
    if isinstance(value, str) and value.endswith("Z"):
        try:
            return _utc_naive(value)
        except ValueError:
            return value
    if isinstance(value, dict):
        return {name: _decode(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _sdk_day(raw: object, trading_date: object):
    if not isinstance(raw, dict):
        raise ValueError("Эмулятор не вернул исходный TradingDay")
    from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

    values = {
        name: _decode(raw.get(name))
        for name in TradingDay.__annotations__
        if name in raw and name != "intervals"
    }
    if "date" not in values:
        try:
            values["date"] = datetime.combine(date.fromisoformat(str(trading_date)), time.min, tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("Эмулятор не вернул дату TradingDay") from exc
    intervals = [
        TradingInterval(
            type=item.get("type", ""),
            interval=TimeInterval(
                start_ts=_decode(item.get("interval", {}).get("start_ts")),
                end_ts=_decode(item.get("interval", {}).get("end_ts")),
            ),
        )
        for item in raw.get("intervals", ())
    ]
    return TradingDay(**values, intervals=intervals)
