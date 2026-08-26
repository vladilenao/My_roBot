from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.bb.signalEnum import BbSignalEnum


@dataclass(frozen=True)
class BollingerBandsIndicator(Indicator):
    """Bollinger Bands индикатор."""

    length: int = 20
    std: float = 2.0

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        return "bb_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        return BbSignalEnum

    @property
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""
        return self.length

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(
                f"Bollinger Bands length ({self.length}) должен быть > 0"
            )
        if self.std <= 0:
            raise ValueError(
                f"Bollinger Bands std ({self.std}) должен быть > 0"
            )

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data.ta.bbands(
            append=True,
            colprefix="bb",
            length=self.length,
            std=self.std,
            close="close",
        )
        data.columns = data.columns.str.lower()

        bb_lower_col = f"bbl_{self.length}_{self.std}"
        bb_upper_col = f"bbu_{self.length}_{self.std}"

        data[self.signal_column] = np.where(
            data["close"] <= data[bb_lower_col],
            BbSignalEnum.TOUCH_LOWER,
            np.where(
                data["close"] >= data[bb_upper_col],
                BbSignalEnum.TOUCH_UPPER,
                BbSignalEnum.NO_SIGNAL,
            ),
        )
        return data
