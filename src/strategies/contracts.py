from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import pandas as pd

from src.strategies.names import StrategyName

DEFAULT_FILTER_PROFILE = "basic_levels"


@dataclass(frozen=True)
class Assignment:
    """Глобально идентифицируемая привязка входной стратегии к профилю управления."""

    id: str
    strategy: StrategyName
    management: str
    filter_profile: str = DEFAULT_FILTER_PROFILE
    priority: int = 0
    timeframe: str = field(kw_only=True)


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    """Неизменяемое входное событие стратегии, не план и не торговая команда."""

    signal_type: SignalType
    price: float
    bar_time: pd.Timestamp | None = None
    event_id: str | None = None
    available_at: pd.Timestamp | None = None
    timeframe: str | None = None
    strategy_name: str | None = None
    indicator_values: dict[str, float] | None = None
    idea_references: dict[str, float | str] | None = None


class Strategy(Protocol):
    NAME: str
    STRATEGY_WINDOW: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision: ...

    def required_history(self) -> int: ...
