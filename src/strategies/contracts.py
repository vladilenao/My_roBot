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


class Strategy(Protocol):
    NAME: str
    STRATEGY_WINDOW: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision: ...

    def required_history(self) -> int: ...
