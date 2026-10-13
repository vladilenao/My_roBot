"""Pure trading-calendar contracts and serialization helpers."""

from .intervals import (
    GENERIC_INTERVAL_TYPES,
    INTERVAL_KIND_TABLE,
    INTERVAL_KIND_TABLE_VERSION,
    IntervalKind,
    exchange_date,
    normalize_exchange,
    SessionWindow,
    interval_kind,
    trading_date,
    windows_for_day,
)
from .core import (
    CalendarDay,
    CandleExpectation,
    DueBar,
    MarketPhase,
    NextEvent,
    ScheduleRef,
    TradingCalendar,
    expect,
    expected_bars,
    scheduled_closed_eligible,
)
from .rules import CANDLE_RULES, CandleRule, candle_rule
from .serialization import canonical_day_json, day_version

__all__ = [
    "CANDLE_RULES",
    "GENERIC_INTERVAL_TYPES",
    "INTERVAL_KIND_TABLE",
    "INTERVAL_KIND_TABLE_VERSION",
    "CandleRule",
    "CalendarDay",
    "CandleExpectation",
    "DueBar",
    "IntervalKind",
    "MarketPhase",
    "NextEvent",
    "ScheduleRef",
    "SessionWindow",
    "TradingCalendar",
    "candle_rule",
    "canonical_day_json",
    "day_version",
    "exchange_date",
    "expect",
    "expected_bars",
    "interval_kind",
    "normalize_exchange",
    "scheduled_closed_eligible",
    "trading_date",
    "windows_for_day",
]
