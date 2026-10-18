"""Рубеж близкой экспирации в боевом режиме: сдвиг из перерыва по расписанию брокера."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from src.market_calendar.core import CalendarDay, TradingCalendar
from src.market_calendar.intervals import SessionWindow
from src.trade_management.manager import _expires_within

MARGIN = timedelta(minutes=20)
# Tinkoff отдаёт экспирацию полуночью дня: календарный рубеж за 2 суток — ночь на воскресенье.
CONTRACT = SimpleNamespace(expiration_date=datetime(2025, 6, 30))


def _day(day: date, trading: bool) -> CalendarDay:
    windows = (SessionWindow(datetime(day.year, day.month, day.day, 6), datetime(day.year, day.month, day.day, 20, 50),
                             "continuous", "regular_trading_session"),) if trading else ()
    return CalendarDay(day, trading, windows, f"v-{day}", datetime(2025, 6, 1))


BROKER = TradingCalendar("tbank", "forts_futures", (
    _day(date(2025, 6, 27), True), _day(date(2025, 6, 28), False), _day(date(2025, 6, 29), False),
    _day(date(2025, 6, 30), True)))


def test_cutoff_in_weekend_fires_before_friday_session_end():
    assert _expires_within(CONTRACT, datetime(2025, 6, 27, 20, 29), 2, BROKER, MARGIN) is False
    assert _expires_within(CONTRACT, datetime(2025, 6, 27, 20, 30), 2, BROKER, MARGIN) is True


def test_without_calendar_the_calendar_threshold_is_unchanged():
    assert _expires_within(CONTRACT, datetime(2025, 6, 27, 23, 59), 2) is False
    assert _expires_within(CONTRACT, datetime(2025, 6, 28), 2) is True


def test_missing_expiration_never_expires_even_with_calendar():
    assert _expires_within(SimpleNamespace(expiration_date=None), datetime(2025, 6, 29), 2, BROKER, MARGIN) is False
