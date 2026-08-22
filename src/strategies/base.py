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


class Strategy(Protocol):
    NAME: str
    STRATEGY_WINDOW: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...

    def decide(self, ta: pd.DataFrame) -> Decision: ...

    def expected_events(self, ta: pd.DataFrame) -> pd.DataFrame: ...

    def required_history(self) -> int: ...
