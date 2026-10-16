"""Расписание брокера 16–19.10.2026 (UTC) для тестов сессионной защиты.

Типы и границы окон — как в ответе TradingSchedules, сохранённом роботом:
акции (`moex_mrng_evng_e_wknd_dlr`) и фьючерсы (`forts_futures_weekend`).
"""

from datetime import date, datetime, timedelta, timezone

from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar

OBSERVED_AT = datetime(2026, 10, 15, tzinfo=timezone.utc)
MSK = timedelta(hours=3)


def msk(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Московское время дня октября 2026 как naive UTC."""
    return datetime(2026, 10, day, hour, minute, second) - MSK


def _window(raw_type: str, start: datetime, end: datetime) -> SessionWindow:
    kind = "auction" if "auction" in raw_type else "clearing" if raw_type == "clearing" else "continuous"
    return SessionWindow(start, end, kind, raw_type)


def _weekday(day: int, *, shares: bool) -> CalendarDay:
    windows = [
        _window("opening_auction_morning", msk(day, 6, 50), msk(day, 6, 59, 59)),
        _window("regular_trading_session_morning", msk(day, 7), msk(day, 8, 59, 59)),
    ]
    if shares:
        windows += [
            _window("regular_trading_session_main", msk(day, 9), msk(day, 18, 54, 59)),
            _window("closing_auction_main", msk(day, 18, 55), msk(day, 18, 59, 30)),
            _window("regular_trading_session_evening", msk(day, 19, 0, 1), msk(day, 23, 49, 59)),
        ]
    else:
        windows += [
            _window("regular_trading_session_main", msk(day, 9), msk(day, 18, 59, 59)),
            _window("regular_trading_session_evening", msk(day, 19), msk(day, 23, 50)),
            _window("clearing", msk(day, 23, 50), msk(day, 23, 59, 59)),
        ]
    return CalendarDay(date(2026, 10, day), True, tuple(windows), f"v-{day}", OBSERVED_AT)


def _weekend(day: int) -> CalendarDay:
    windows = (
        _window("holiday_opening_auction_morning", msk(day, 9, 50), msk(day, 9, 59, 59)),
        _window("holiday_trading_session_morning", msk(day, 10), msk(day, 19)),
    )
    return CalendarDay(date(2026, 10, day), True, windows, f"v-{day}", OBSERVED_AT)


def calendar(*, shares: bool, days: tuple[int, ...] = (15, 16, 17, 18, 19)) -> TradingCalendar:
    built = [_weekend(day) if day in (17, 18) else _weekday(day, shares=shares) for day in days]
    exchange = "moex_mrng_evng_e_wknd_dlr" if shares else "forts_futures_weekend"
    return TradingCalendar("tbank", exchange, tuple(built))


SHARES = calendar(shares=True)
FUTURES = calendar(shares=False)
