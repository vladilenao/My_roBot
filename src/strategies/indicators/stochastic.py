from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.strategies.indicators.base import Indicator


@dataclass(frozen=True)
class StochasticIndicator(Indicator):
    """Stochastic осциллятор с конфигурируемыми параметрами.

    Параметры по умолчанию соответствуют текущему хардкоду:
    k=14, d=3, smooth_k=3.
    """

    k: int = 14
    d: int = 3
    smooth_k: int = 3
    signal_column: str = "stoch_signal"

    @property
    def warmup(self) -> int:
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

        data[self.signal_column] = np.where(
            (data[stoch_k_col] > data[stoch_k_col].shift(1))
            & (data[stoch_k_col].shift(1) < 20)
            & (data[stoch_k_col] > 20)
            & (data[stoch_k_col] < 50),
            1,
            np.where(
                (data[stoch_k_col] < data[stoch_k_col].shift(1))
                & (data[stoch_k_col].shift(1) > 80)
                & (data[stoch_k_col] < 80)
                & (data[stoch_k_col] > 50),
                -1,
                0,
            ),
        )
        return data


class StochasticIndicatorBuilder:
    """Builder для StochasticIndicator с method chaining."""

    def __init__(self) -> None:
        self._k: int = 14
        self._d: int = 3
        self._smooth_k: int = 3

    def set_k(self, val: int) -> StochasticIndicatorBuilder:
        self._k = val
        return self

    def set_d(self, val: int) -> StochasticIndicatorBuilder:
        self._d = val
        return self

    def set_smooth_k(self, val: int) -> StochasticIndicatorBuilder:
        self._smooth_k = val
        return self

    def build(self) -> StochasticIndicator:
        return StochasticIndicator(k=self._k, d=self._d, smooth_k=self._smooth_k)
