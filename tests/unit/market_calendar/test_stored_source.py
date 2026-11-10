"""Контур читает расписание, которое пишет ядро: кэш не замерзает на моменте запуска.

Случай ПРОМ 10.11.2026: контур запущен 07.11 в 14:25 UTC, ядро каждый час
подтверждало расписание, а контур со второго дня после запуска видел
«расписание неизвестно» и отклонял внутридневные входы.
"""

from datetime import date, datetime, timedelta, timezone

from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

from src.data.market_store import MarketDataStore
from src.market_calendar import CANDLE_RULES, MarketPhase
from src.market_calendar.service import MarketCalendar, StoredScheduleSource

EXCHANGE = "forts_futures_weekend"
UID = "uid-brz6"
MAX_AGE = 129600  # 36 ч, как schedule_max_age_secs по умолчанию


def _day(value: date) -> TradingDay:
    start = datetime(value.year, value.month, value.day, 6, tzinfo=timezone.utc)
    return TradingDay(
        date=datetime(value.year, value.month, value.day, tzinfo=timezone.utc),
        is_trading_day=True,
        intervals=[TradingInterval(type="regular_trading_session_main",
                                   interval=TimeInterval(start_ts=start, end_ts=start + timedelta(hours=10)))],
    )


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _core_writes(store: MarketDataStore, days, observed_at: datetime) -> None:
    """Ядро: часовой запрос расписания режима — запись или подтверждение дней."""
    for value in days:
        store.record_schedule_day(source="tbank", exchange=EXCHANGE, day=_day(value), observed_at=observed_at)


def _contour(tmp_path, clock, horizon=9):
    core = MarketDataStore(tmp_path / "market-data.sqlite3")
    started = datetime(2026, 11, 7, 14, 25)
    days = [date(2026, 11, 7) + timedelta(days=offset) for offset in range(horizon)]
    core.record_schedule_binding(source="tbank", instrument_uid=UID, exchange=EXCHANGE, observed_at=started)
    _core_writes(core, days, started - timedelta(minutes=10))
    # Контур открывает ту же базу своим подключением, как процесс контура.
    contour_store = MarketDataStore(tmp_path / "market-data.sqlite3")
    calendar = MarketCalendar(contour_store, StoredScheduleSource(contour_store, clock=clock),
                              schedule_max_age_secs=MAX_AGE)
    return core, calendar, started, days


def _phase(calendar: MarketCalendar, now: datetime) -> MarketPhase:
    return calendar.expectation(UID, CANDLE_RULES["5m"], now, timedelta(seconds=65)).phase


def test_contour_sees_core_confirmations_after_start(tmp_path):
    clock = Clock()
    core, calendar, started, days = _contour(tmp_path, clock)
    assert _phase(calendar, datetime(2026, 11, 7, 15)) is not MarketPhase.UNKNOWN

    # Ядро подтверждает расписание каждый час; контур продолжает работать.
    moment = started
    while moment < datetime(2026, 11, 10, 9):
        moment += timedelta(hours=1)
        clock.value += 3600
        _core_writes(core, days, moment)
        calendar.calendar_for(UID, moment)

    now = datetime(2026, 11, 10, 9)
    assert calendar.calendar_for(UID, now).day(date(2026, 11, 10)) is not None
    assert _phase(calendar, now) is MarketPhase.TRADING


def test_contour_sees_new_day_written_by_core(tmp_path):
    clock = Clock()
    core, calendar, started, days = _contour(tmp_path, clock, horizon=2)
    future = date(2026, 11, 9)
    assert calendar.calendar_for(UID, started).day(future) is None

    _core_writes(core, [future], started + timedelta(hours=10))
    clock.value += 61
    assert calendar.calendar_for(UID, started + timedelta(hours=10)).day(future) is not None


def test_contour_checks_store_at_most_once_per_poll_interval(tmp_path):
    clock = Clock()
    core, calendar, started, days = _contour(tmp_path, clock, horizon=2)
    future = date(2026, 11, 9)
    assert calendar.calendar_for(UID, started).day(future) is None

    _core_writes(core, [future], started + timedelta(minutes=1))
    clock.value += 30
    # Внутри интервала опроса — прежний снимок, без лишних чтений базы на каждой паре.
    assert calendar.calendar_for(UID, started).day(future) is None
    clock.value += 31
    assert calendar.calendar_for(UID, started).day(future) is not None


def test_contour_without_core_confirmations_loses_schedule(tmp_path):
    """Ядро перестало подтверждать — применимость истекает, как и задумано (не последняя известная фаза)."""
    clock = Clock()
    core, calendar, started, days = _contour(tmp_path, clock)
    clock.value += 3 * 86400
    assert _phase(calendar, datetime(2026, 11, 10, 9)) is MarketPhase.UNKNOWN
