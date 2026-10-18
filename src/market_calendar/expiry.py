"""Эффективный рубеж экспирации: календарный срок, сдвинутый из перерыва на конец сессии.

Календарный рубеж (N суток по 24 часа до границы контракта) внутри торгового
окна остаётся как есть. Попавший в перерыв рубеж сдвигается назад — на конец
последнего торгового окна минус запас, чтобы закрытие исполнилось в этой
сессии, а не на открытии следующей. Сдвиг применяется, только если расписание
знает каждую торговую дату от последнего окна до рубежа; иначе действует
календарный рубеж.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .core import TradingCalendar, _trading_windows
from .intervals import exchange_date

CUTOFF_CALENDAR = "calendar"
CUTOFF_SESSION_END = "session-end"
CUTOFF_SCHEDULE_UNKNOWN = "schedule-unknown"


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ExpiryCutoff:
    """Эффективный и календарный рубежи (naive UTC) и происхождение эффективного."""

    at: datetime
    calendar_at: datetime
    source: str
    session_end: datetime | None = None


def expiry_cutoff(
    calendar: TradingCalendar | None, calendar_at: datetime, close_before_end: timedelta,
) -> ExpiryCutoff:
    calendar_at = _naive_utc(calendar_at)
    unknown = ExpiryCutoff(calendar_at, calendar_at, CUTOFF_SCHEDULE_UNKNOWN)
    if calendar is None:
        return unknown
    windows = _trading_windows(calendar)
    if any(window.start <= calendar_at < window.end for _, window in windows):
        return ExpiryCutoff(calendar_at, calendar_at, CUTOFF_CALENDAR)
    previous = [(day, window) for day, window in windows if window.end <= calendar_at]
    if not previous:
        return unknown
    day, window = max(previous, key=lambda item: item[1].end)
    if not _days_known(calendar, day, exchange_date(calendar_at)):
        return unknown
    at = max(window.start, window.end - close_before_end)
    return ExpiryCutoff(at, calendar_at, CUTOFF_SESSION_END, window.end)


def _days_known(calendar: TradingCalendar, first: date, last: date) -> bool:
    current = first
    while current <= last:
        if calendar.day(current) is None:
            return False
        current += timedelta(days=1)
    return True
