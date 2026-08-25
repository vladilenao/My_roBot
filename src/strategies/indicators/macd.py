from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.strategies.indicators.base import Indicator


@dataclass(frozen=True)
class MacdIndicator(Indicator):
    """MACD индикатор с конфигурируемыми параметрами.

    Параметры по умолчанию соответствуют текущему хардкоду:
    fast=12, slow=26, signal=9.
    """

    fast: int = 12
    slow: int = 26
    signal: int = 9
    signal_column: str = "macd_signal"

    @property
    def warmup(self) -> int:
        return self.slow + self.signal

    def __post_init__(self) -> None:
        if self.fast >= self.slow:
            raise ValueError(
                f"MACD fast ({self.fast}) должен быть < slow ({self.slow})"
            )
        if self.signal >= self.slow:
            raise ValueError(
                f"MACD signal ({self.signal}) должен быть < slow ({self.slow})"
            )

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data.ta.macd(
            append=True,
            colprefix="macd",
            fast=self.fast,
            slow=self.slow,
            signal=self.signal,
            close="close",
        )
        data.columns = data.columns.str.lower()

        macd_col = f"macd_{self.fast}_{self.slow}_{self.signal}"
        macds_col = f"macds_{self.fast}_{self.slow}_{self.signal}"

        data[self.signal_column] = np.where(
            (data[macds_col] > data[macds_col].shift(1))
            & (data[macds_col] > data[macd_col].shift(1))
            & (data[macd_col] < 0)
            & (data[macds_col] < 0),
            1,
            np.where(
                (data[macds_col] < data[macds_col].shift(1))
                & (data[macds_col] < data[macd_col].shift(1))
                & (data[macd_col] > 0)
                & (data[macds_col] > 0),
                -1,
                0,
            ),
        )
        return data


class MacdIndicatorBuilder:
    """Builder для MacdIndicator с method chaining."""

    def __init__(self) -> None:
        self._fast: int = 12
        self._slow: int = 26
        self._signal: int = 9

    def set_fast(self, val: int) -> MacdIndicatorBuilder:
        self._fast = val
        return self

    def set_slow(self, val: int) -> MacdIndicatorBuilder:
        self._slow = val
        return self

    def set_signal(self, val: int) -> MacdIndicatorBuilder:
        self._signal = val
        return self

    def build(self) -> MacdIndicator:
        return MacdIndicator(fast=self._fast, slow=self._slow, signal=self._signal)
