from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.rsi.signalEnum import RsiSignalEnum


@dataclass(frozen=True)
class RsiIndicator(Indicator):
    """RSI индикатор с обязательным периодом."""

    period: int

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        return "rsi_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        return RsiSignalEnum

    @property
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""
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
            RsiSignalEnum.CROSS_ABOVE_50,
            np.where(
                (data["rsi"] < 50) & (data["rsi"].shift(1) > 50),
                RsiSignalEnum.CROSS_BELOW_50,
                RsiSignalEnum.NO_SIGNAL,
            ),
        )
        return data
