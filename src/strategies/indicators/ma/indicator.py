from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.strategies.indicators.base import BaseSignalEnum, Indicator
from src.strategies.indicators.ma.signalEnum import MaCloudSignalEnum
from src.logging_setup import get_logger
from src.trade_management.audit import CalculationTrace, MeasuredValue, calculation_trace

log = get_logger(__name__)


@dataclass(frozen=True)
class MaCloudIndicator(Indicator):
    """MA Cloud индикатор: пересечение SMA fast и SMA slow."""

    fast_period: int = 10
    slow_period: int = 40

    @property
    def signal_column(self) -> str:
        """Имя столбца сигнала."""
        return "ma_cloud_signal"

    @property
    def signal_enum(self) -> type[BaseSignalEnum]:
        """Перечень возможных сигналов данного индикатора."""
        return MaCloudSignalEnum

    @property
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""
        return self.slow_period

    def __post_init__(self) -> None:
        if self.fast_period <= 0:
            raise ValueError(f"MA fast_period ({self.fast_period}) должен быть > 0")
        if self.slow_period <= 0:
            raise ValueError(f"MA slow_period ({self.slow_period}) должен быть > 0")
        if self.fast_period >= self.slow_period:
            raise ValueError(
                f"MA fast_period ({self.fast_period}) должен быть < slow_period ({self.slow_period})"
            )

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data["sma_fast"] = ta.sma(data["close"], length=self.fast_period)
        data["sma_slow"] = ta.sma(data["close"], length=self.slow_period)
        data["sma_fast_available"] = data["sma_fast"].notna()
        data["sma_slow_available"] = data["sma_slow"].notna()

        data[self.signal_column] = np.where(
            (data["sma_fast"] > data["sma_slow"])
            & (data["sma_fast"].shift(1) <= data["sma_slow"].shift(1)),
            MaCloudSignalEnum.MA_CROSS_UP,
            np.where(
                (data["sma_fast"] < data["sma_slow"])
                & (data["sma_fast"].shift(1) >= data["sma_slow"].shift(1)),
                MaCloudSignalEnum.MA_CROSS_DOWN,
                MaCloudSignalEnum.NO_SIGNAL,
            ),
        )
        return data

    def compute_with_trace(self, df: pd.DataFrame) -> tuple[pd.DataFrame, CalculationTrace]:
        """Compute SMA cloud with the complete local close windows used by it."""
        data = self.compute(df)
        close = data["close"].tail(self.slow_period).tolist()
        fast, slow = data["sma_fast"].iloc[-1], data["sma_slow"].iloc[-1]
        return data, calculation_trace(
            "indicator.ma_cloud", inputs={
                "fast_period": MeasuredValue(self.fast_period, "bars"),
                "slow_period": MeasuredValue(self.slow_period, "bars"),
                "closed_closes": MeasuredValue(close, "price"),
            }, result=MeasuredValue({"sma_fast": None if pd.isna(fast) else float(fast),
                                      "sma_slow": None if pd.isna(slow) else float(slow)}, "moving-averages"),
            reason="moving-averages-available" if not pd.isna(slow) else "insufficient-history",
            formula="SMA_n=sum(last n closed prices)/n; cloud uses SMA fast and slow",
        )
