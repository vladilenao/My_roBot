from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.macd.signalEnum import MacdSignalEnum


@dataclass(frozen=True)
class MacdIndicator(Indicator):
    """MACD индикатор с обязательными параметрами."""

    fast: int
    slow: int
    signal: int

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        return "macd_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        return MacdSignalEnum

    @property
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""
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
            MacdSignalEnum.BULLISH_CROSSOVER_BELOW_ZERO,
            np.where(
                (data[macds_col] < data[macds_col].shift(1))
                & (data[macds_col] < data[macd_col].shift(1))
                & (data[macd_col] > 0)
                & (data[macds_col] > 0),
                MacdSignalEnum.BEARISH_CROSSOVER_ABOVE_ZERO,
                MacdSignalEnum.NO_SIGNAL,
            ),
        )
        return data
