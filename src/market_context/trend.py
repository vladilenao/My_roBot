from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from src.market_context.models import TrendDirection, TrendResult


@dataclass(frozen=True)
class TrendAnalyzer:
    """Статистически чистый (stateless) анализатор направления и силы тренда.

    Направление определяется пересечением короткой и длинной EMA;
    сила — нормализованным ADX.
    """

    ema_short: int = 20
    ema_long: int = 50
    adx_period: int = 14

    def __post_init__(self) -> None:
        if self.ema_short <= 0 or self.ema_long <= 0 or self.adx_period <= 0:
            raise ValueError("Параметры тренда должны быть положительными")

    def analyze(self, df: pd.DataFrame) -> TrendResult:
        if df.empty:
            return TrendResult(TrendDirection.FLAT, 0.0)

        short_col = self.ema_series(df["close"], self.ema_short)
        long_col = self.ema_series(df["close"], self.ema_long)

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=self.adx_period)
        adx_col = adx_df[f"ADX_{self.adx_period}"].to_numpy() if adx_df is not None else np.full(len(df), np.nan)

        last_short = self._last_valid(short_col)
        last_long = self._last_valid(long_col)
        last_adx = self._last_valid(adx_col)

        if last_adx is None or last_adx < 25.0:
            return TrendResult(
                direction=TrendDirection.FLAT,
                strength=0.0,
                ema_short=last_short,
                ema_long=last_long,
            )

        if last_short is not None and last_long is not None and last_short > last_long:
            direction = TrendDirection.UP
        elif last_short is not None and last_long is not None and last_short < last_long:
            direction = TrendDirection.DOWN
        else:
            direction = TrendDirection.FLAT

        strength = self._normalize_adx(last_adx)

        return TrendResult(
            direction=direction,
            strength=strength,
            ema_short=last_short,
            ema_long=last_long,
        )

    def ema_series(self, close: pd.Series, length: int) -> pd.Series:
        return ta.ema(close, length=length)

    @staticmethod
    def _last_valid(values) -> float | None:
        series = pd.Series(values)
        last = series.dropna()
        if last.empty:
            return None
        return float(last.iloc[-1])

    @staticmethod
    def _normalize_adx(adx: float) -> float:
        return float(np.clip((adx - 25.0) / (60.0 - 25.0), 0.0, 1.0))
