from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.stochastic.signalEnum import (
    SignalMode,
    StochasticSignalEnum,
)


@dataclass(frozen=True)
class StochasticIndicator(Indicator):
    """Stochastic осциллятор с обязательными параметрами."""

    k: int
    d: int
    smooth_k: int
    signal_mode: SignalMode = SignalMode.EXIT_FROM_ZONES

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        return "stoch_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        return StochasticSignalEnum

    @property
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""
        return self.k + self.smooth_k

    def __post_init__(self) -> None:
        if self.k < self.d:
            raise ValueError(
                f"Stochastic k ({self.k}) должен быть >= d ({self.d})"
            )

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data.ta.stoch(
            append=True,
            colprefix="stoch",
            k=self.k,
            d=self.d,
            smooth_k=self.smooth_k,
            close="close",
        )
        data.columns = data.columns.str.lower()

        stoch_k_col = f"stochk_{self.k}_{self.d}_{self.smooth_k}"
        stoch_d_col = f"stochd_{self.k}_{self.d}_{self.smooth_k}"

        if self.signal_mode is SignalMode.KD_CROSSOVER:
            data[self.signal_column] = np.where(
                (data[stoch_k_col] > data[stoch_d_col])
                & (data[stoch_k_col].shift(1) <= data[stoch_d_col].shift(1)),
                StochasticSignalEnum.CROSS_UP,
                np.where(
                    (data[stoch_k_col] < data[stoch_d_col])
                    & (data[stoch_k_col].shift(1) >= data[stoch_d_col].shift(1)),
                    StochasticSignalEnum.CROSS_DOWN,
                    StochasticSignalEnum.NO_SIGNAL,
                ),
            )
        else:
            data[self.signal_column] = np.where(
                (data[stoch_k_col] > data[stoch_k_col].shift(1))
                & (data[stoch_k_col].shift(1) < 20)
                & (data[stoch_k_col] > 20)
                & (data[stoch_k_col] < 50),
                StochasticSignalEnum.EXIT_OVERSOLD,
                np.where(
                    (data[stoch_k_col] < data[stoch_k_col].shift(1))
                    & (data[stoch_k_col].shift(1) > 80)
                    & (data[stoch_k_col] < 80)
                    & (data[stoch_k_col] > 50),
                    StochasticSignalEnum.EXIT_OVERBOUGHT,
                    StochasticSignalEnum.NO_SIGNAL,
                ),
            )
        return data
