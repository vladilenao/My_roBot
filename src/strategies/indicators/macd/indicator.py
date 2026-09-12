from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.macd.signalEnum import (
    MacdSignalEnum,
    MacdZeroCrossSignalEnum,
)
from src.logging_setup import get_logger

log = get_logger(__name__)


class MacdMode(Enum):
    """Режим интерпретации сигнала MACD-осциллятора.

    Один и тот же осциллятор (fast/slow/signal) даёт разные сигналы:
      * SIGNAL_LINE_CROSS — кроссовер линий (сигнальной и MACD) с фильтром
        зоны: бычий — когда обе линии ниже нуля, медвежий — когда обе выше.
      * ZERO_CROSS — пересечение сигнальной линией нулевого уровня
        (смена знака осциллятора), независимо от взаимного положения линий.
    """

    SIGNAL_LINE_CROSS = "signal_line_cross"
    ZERO_CROSS = "zero_cross"


@dataclass(frozen=True)
class MacdIndicator(Indicator):
    """MACD индикатор с двумя режимами сигнала (см. MacdMode)."""

    fast: int = 12
    slow: int = 26
    signal: int = 9
    mode: MacdMode = MacdMode.SIGNAL_LINE_CROSS

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        if self.mode is MacdMode.ZERO_CROSS:
            return "macd_zero_signal"
        return "macd_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        if self.mode is MacdMode.ZERO_CROSS:
            return MacdZeroCrossSignalEnum
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

        if self.mode is MacdMode.ZERO_CROSS:
            data[self.signal_column] = np.where(
                (data[macds_col] > 0) & (data[macds_col].shift(1) <= 0),
                MacdZeroCrossSignalEnum.MACD_CROSS_ABOVE_ZERO,
                np.where(
                    (data[macds_col] < 0) & (data[macds_col].shift(1) >= 0),
                    MacdZeroCrossSignalEnum.MACD_CROSS_BELOW_ZERO,
                    MacdZeroCrossSignalEnum.NO_SIGNAL,
                ),
            )
        else:
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
