from __future__ import annotations

import pandas as pd

from src.market_context.models import MarketContext
from src.market_context.sr_levels import SRLevelsCalculator
from src.market_context.trend import TrendAnalyzer
from src.logging_setup import get_logger

log = get_logger(__name__)


class MarketContextCache:
    """Ленивый кэш рыночного контекста по парам (инструмент, таймфрейм).

    Контекст вычисляется один раз на пару и пересчитывается только при
    появлении новой закрытой свечи её таймфрейма. Инвалидация по tz-naive
    `datetime` последней строки DataFrame (согласовано с MarketDataCache).
    """

    def __init__(
        self,
        data_cache,
        trend_analyzer: TrendAnalyzer,
        sr_calculator: SRLevelsCalculator,
    ) -> None:
        self._data_cache = data_cache
        self._trend_analyzer = trend_analyzer
        self._sr_calculator = sr_calculator
        self._cache: dict[tuple, tuple[pd.Timestamp, MarketContext]] = {}

    def get_context(self, instrument, timeframe: str) -> MarketContext:
        df = self._data_cache.frame_for(instrument, timeframe)
        key = self._key(instrument, timeframe)
        last_dt = self._last_datetime(df)
        cached = self._cache.get(key)

        if last_dt is None:
            return self._empty_context(instrument, timeframe)

        if cached is not None and cached[0] == last_dt:
            return cached[1]

        context = self._compute_context(df, instrument)
        self._cache[key] = (last_dt, context)
        return context

    def invalidate(self, instrument, timeframe: str) -> None:
        self._cache.pop(self._key(instrument, timeframe), None)

    def _compute_context(self, df: pd.DataFrame, instrument) -> MarketContext:
        trend = self._trend_analyzer.analyze(df)
        current_price = float(df["close"].iloc[-1])
        sr_levels = self._sr_calculator.compute(df, current_price)
        return MarketContext(trend=trend, sr_levels=sr_levels, current_price=current_price)

    def _empty_context(self, instrument, timeframe: str) -> MarketContext:
        df = self._data_cache.frame_for(instrument, timeframe)
        from src.market_context.models import TrendDirection, TrendResult

        current_price = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0
        return MarketContext(
            trend=TrendResult(TrendDirection.FLAT, 0.0),
            sr_levels=[],
            current_price=current_price,
        )

    @staticmethod
    def _key(instrument, timeframe: str) -> tuple:
        base = getattr(instrument, "base_code", None) or getattr(instrument, "ticker", None) or str(instrument)
        return (base, timeframe)

    @staticmethod
    def _last_datetime(df: pd.DataFrame) -> pd.Timestamp | None:
        if df is None or df.empty:
            return None
        last = df["datetime"].iloc[-1]
        return pd.Timestamp(last)
