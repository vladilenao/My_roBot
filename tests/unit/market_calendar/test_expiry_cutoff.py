"""Эффективный рубеж экспирации: календарный, сдвинутый на конец сессии, неизвестный."""

from datetime import date, datetime, timedelta

from src.market_calendar.core import CalendarDay, TradingCalendar
from src.market_calendar.expiry import (
    CUTOFF_CALENDAR, CUTOFF_SCHEDULE_UNKNOWN, CUTOFF_SESSION_END, expiry_cutoff,
)
from src.market_calendar.intervals import SessionWindow

MARGIN = timedelta(minutes=20)
OBSERVED = datetime(2025, 6, 1)


def _weekday(day: date) -> CalendarDay:
    at = lambda hour, minute=0: datetime(day.year, day.month, day.day, hour, minute)  # noqa: E731
    return CalendarDay(day, True, (
        SessionWindow(at(5, 50), at(6), "auction", "opening_auction"),
        SessionWindow(at(6), at(15, 50), "continuous", "regular_trading_session_main"),
        SessionWindow(at(15, 50), at(16, 5), "clearing", "clearing"),
        SessionWindow(at(16, 5), at(20, 50), "continuous", "regular_trading_session_evening"),
    ), f"v-{day}", OBSERVED, "reconstructed", "forts_futures_weekend/2")


def _holiday(day: date) -> CalendarDay:
    return CalendarDay(day, False, (), f"v-{day}", OBSERVED, "reconstructed", "forts_futures_weekend/2")


def _calendar(*days: CalendarDay) -> TradingCalendar:
    return TradingCalendar("emulator", "forts_futures_weekend", days)


FRIDAY, SATURDAY, SUNDAY, MONDAY = (date(2025, 6, 27), date(2025, 6, 28), date(2025, 6, 29), date(2025, 6, 30))
WEEKEND = _calendar(_weekday(FRIDAY), _holiday(SATURDAY), _holiday(SUNDAY), _weekday(MONDAY))


def test_sunday_cutoff_moves_back_to_friday_session_end_minus_margin():
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 29, 16, 5), MARGIN)
    assert (result.at, result.source, result.session_end) == (
        datetime(2025, 6, 27, 20, 30), CUTOFF_SESSION_END, datetime(2025, 6, 27, 20, 50))
    assert result.calendar_at == datetime(2025, 6, 29, 16, 5)


def test_cutoff_inside_session_stays_calendar():
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 30, 16, 5), MARGIN)
    assert (result.at, result.source, result.session_end) == (datetime(2025, 6, 30, 16, 5), CUTOFF_CALENDAR, None)


def test_cutoff_in_clearing_moves_to_end_of_main_session():
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 30, 16, 0), MARGIN)
    assert (result.at, result.source) == (datetime(2025, 6, 30, 15, 30), CUTOFF_SESSION_END)


def test_cutoff_in_opening_auction_counts_as_break():
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 30, 5, 55), MARGIN)
    assert (result.at, result.source) == (datetime(2025, 6, 27, 20, 30), CUTOFF_SESSION_END)


def test_missing_day_between_session_and_cutoff_keeps_calendar_cutoff():
    gap = _calendar(_weekday(FRIDAY), _holiday(SUNDAY), _weekday(MONDAY))
    result = expiry_cutoff(gap, datetime(2025, 6, 29, 16, 5), MARGIN)
    assert (result.at, result.source) == (datetime(2025, 6, 29, 16, 5), CUTOFF_SCHEDULE_UNKNOWN)


def test_no_calendar_or_no_earlier_session_keeps_calendar_cutoff():
    assert expiry_cutoff(None, datetime(2025, 6, 29, 16, 5), MARGIN).source == CUTOFF_SCHEDULE_UNKNOWN
    only_weekend = _calendar(_holiday(SATURDAY), _holiday(SUNDAY))
    assert expiry_cutoff(only_weekend, datetime(2025, 6, 29, 16, 5), MARGIN).source == CUTOFF_SCHEDULE_UNKNOWN


def test_margin_longer_than_session_stops_at_session_start():
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 29, 16, 5), timedelta(hours=10))
    assert result.at == datetime(2025, 6, 27, 16, 5)
    assert result.at < result.calendar_at


def test_aware_cutoff_is_normalized_to_naive_utc():
    from datetime import timezone
    result = expiry_cutoff(WEEKEND, datetime(2025, 6, 29, 16, 5, tzinfo=timezone.utc), MARGIN)
    assert result.at == datetime(2025, 6, 27, 20, 30)
