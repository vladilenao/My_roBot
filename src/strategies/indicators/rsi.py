from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.strategies.indicators.base import Indicator


@dataclass(frozen=True)
class RsiIndicator(Indicator):
    """RSI индикатор с конфигурируемым периодом.

    Период по умолчанию: 14 (соответствует текущему хардкоду).
    """

    period: int = 14
    signal_column: str = "rsi_signal"

    @property
    def warmup(self) -> int:
        return self.period

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError(
                f"RSI period ({self.period}) должен быть > 0"
            )

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data["rsi"] = ta.rsi(data["close"], length=self.period)

        data[self.signal_column] = np.where(
            (data["rsi"] > 50) & (data["rsi"].shift(1) < 50),
            1,
            np.where(
                (data["rsi"] < 50) & (data["rsi"].shift(1) > 50),
                -1,
                0,
            ),
        )
        return data


class RsiIndicatorBuilder:
    """Builder для RsiIndicator с method chaining."""

    def __init__(self) -> None:
        self._period: int = 14

    def set_period(self, val: int) -> RsiIndicatorBuilder:
        self._period = val
        return self

    def build(self) -> RsiIndicator:
        return RsiIndicator(period=self._period)
