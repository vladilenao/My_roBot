from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrendDirection(Enum):
    """Направление тренда."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


@dataclass(frozen=True)
class TrendResult:
    """Результат анализа тренда. Заморожен — создаётся один раз за тик."""

    direction: TrendDirection
    strength: float
    ema_short: float | None = None
    ema_long: float | None = None


class SRType(Enum):
    """Тип уровня S/R."""

    SUPPORT = "support"
    RESISTANCE = "resistance"


@dataclass(frozen=True)
class SRLevel:
    """Горизонтальный уровень поддержки/сопротивления."""

    price: float
    sr_type: SRType
    strength: int
    label: str


@dataclass(frozen=True)
class MarketContext:
    """Рыночный контекст инструмента на текущий тик."""

    trend: TrendResult
    sr_levels: list[SRLevel]
    current_price: float
