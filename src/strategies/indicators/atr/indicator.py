from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.trade_management.audit import CalculationTrace, MeasuredValue, calculation_trace


@dataclass(frozen=True)
class AtrWilderIndicator:
    """Wilder ATR over closed OHLC bars."""

    period: int = 14

    @property
    def warmup(self) -> int:
        return self.period

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError(f"ATR period ({self.period}) должен быть > 0")

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        previous_close = data["close"].shift(1)
        data["true_range"] = pd.concat(
            (
                data["high"] - data["low"],
                (data["high"] - previous_close).abs(),
                (data["low"] - previous_close).abs(),
            ),
            axis=1,
        ).max(axis=1)

        atr = pd.Series(np.nan, index=data.index, dtype=float)
        if len(data) >= self.period:
            atr.iloc[self.period - 1] = data["true_range"].iloc[: self.period].mean()
            for index in range(self.period, len(data)):
                atr.iloc[index] = (
                    (self.period - 1) * atr.iloc[index - 1]
                    + data["true_range"].iloc[index]
                ) / self.period
        data["atr_wilder"] = atr
        data["atr_available"] = atr.notna()
        return data

    def compute_with_trace(self, df: pd.DataFrame) -> tuple[pd.DataFrame, CalculationTrace]:
        """Compute ATR and retain the closed OHLC window and Wilder seed."""
        data = self.compute(df)
        window = data[["high", "low", "close"]].tail(self.period).to_dict("records")
        tr = data["true_range"].tail(self.period).tolist()
        atr = data["atr_wilder"].iloc[-1] if len(data) else float("nan")
        return data, calculation_trace(
            "indicator.atr_wilder", inputs={
                "period": MeasuredValue(self.period, "bars"),
                "closed_ohlc": MeasuredValue(window, "OHLC"),
                "true_ranges": MeasuredValue(tr, "price"),
            }, result=MeasuredValue(None if pd.isna(atr) else float(atr), "price"),
            reason="atr-available" if not pd.isna(atr) else "insufficient-history",
            formula="TR=max(high-low,abs(high-prev_close),abs(low-prev_close)); seed=SMA(TR); Wilder recurrence",
        )
