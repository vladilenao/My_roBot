"""Правила формирования свечи по таймфрейму и признак подтверждённости.

Границы подтверждены свечами брокера (фикстура `candle_boundaries_*`) и
совпадают с сеткой ``CandleScheduler``. Неподтверждённое правило обязано
возвращать ``rule_confirmed=False``, чтобы не давать календарного
доказательства планового отсутствия свечи.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Boundary = Literal["utc_grid", "utc_midnight", "utc_week_monday", "utc_month_first"]


@dataclass(frozen=True)
class CandleRule:
    timeframe: str
    boundary: Boundary
    rule_confirmed: bool
    evidence: str


CANDLE_RULES: dict[str, CandleRule] = {
    "1m": CandleRule("1m", "utc_grid", True, "UTC-сетка; действующий код и ingestion 1.3"),
    "5m": CandleRule("5m", "utc_grid", True, "UTC-сетка; действующий код и ingestion 1.3"),
    "15m": CandleRule("15m", "utc_grid", True, "UTC-сетка; действующий код и ingestion 1.3"),
    "30m": CandleRule("30m", "utc_grid", True, "UTC-сетка; действующий код и ingestion 1.3"),
    "1h": CandleRule("1h", "utc_grid", True, "UTC-сетка; действующий код и ingestion 1.3"),
    "4h": CandleRule("4h", "utc_grid", True, "сетка от 00:00 UTC; подтверждено свечами брокера"),
    "1d": CandleRule("1d", "utc_midnight", True, "календарные сутки UTC; подтверждено свечами брокера"),
    "1w": CandleRule("1w", "utc_week_monday", True, "начало недели — понедельник 00:00 UTC; подтверждено свечами брокера"),
    "1M": CandleRule("1M", "utc_month_first", True, "начало месяца — 1-е число 00:00 UTC; подтверждено свечами брокера"),
}


def candle_rule(timeframe: str) -> CandleRule:
    try:
        return CANDLE_RULES[timeframe]
    except KeyError:
        raise ValueError(f"Нет правила свечи для ТФ {timeframe!r}") from None
