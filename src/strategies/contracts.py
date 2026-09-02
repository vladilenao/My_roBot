from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import pandas as pd


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    signal_type: SignalType
    price: float
    timeframe: str | None = None
    strategy_name: str | None = None
    indicator_values: dict[str, float] | None = None
    bar_time: pd.Timestamp | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_distance_pct: float | None = None
    tp_distance_pct: float | None = None
    sl_level_label: str | None = None
    tp_level_label: str | None = None
    trend_direction: str | None = None
    trend_confidence: float | None = None


class Strategy(Protocol):
    NAME: str
    STRATEGY_WINDOW: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision: ...

    def required_history(self) -> int: ...
