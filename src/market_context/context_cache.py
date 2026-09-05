from __future__ import annotations

import pandas as pd

from src.market_context.models import MarketContext
from src.market_context.sr_levels import SRLevelsCalculator
from src.market_context.trend import TrendAnalyzer


class MarketContextCache:
    """Ленивый кэш рыночного контекста по инструментам.

    Контекст вычисляется один раз на инструмент и пересчитывается только при
    появлении новой закрытой свечи. Инвалидация по tz-naive `datetime` последней
    строки DataFrame (согласовано с MarketDataCache).
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
        self._cache: dict[str, tuple[pd.Timestamp, MarketContext]] = {}

    def get_context(self, instrument) -> MarketContext:
        df = self._data_cache.frame_for(instrument)
        key = self._key(instrument)
        last_dt = self._last_datetime(df)
        cached = self._cache.get(key)

        if last_dt is None:
            return self._empty_context(instrument)

        if cached is not None and cached[0] == last_dt:
            return cached[1]

        context = self._compute_context(df, instrument)
        self._cache[key] = (last_dt, context)
        return context

    def invalidate(self, instrument) -> None:
        self._cache.pop(self._key(instrument), None)

    def _compute_context(self, df: pd.DataFrame, instrument) -> MarketContext:
        trend = self._trend_analyzer.analyze(df)
        current_price = float(df["close"].iloc[-1])
        sr_levels = self._sr_calculator.compute(df, current_price)
        return MarketContext(trend=trend, sr_levels=sr_levels, current_price=current_price)

    def _empty_context(self, instrument) -> MarketContext:
        df = self._data_cache.frame_for(instrument)
        from src.market_context.models import TrendDirection, TrendResult

        current_price = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0
        return MarketContext(
            trend=TrendResult(TrendDirection.FLAT, 0.0),
            sr_levels=[],
            current_price=current_price,
        )

    @staticmethod
    def _key(instrument) -> str:
        return getattr(instrument, "base_code", None) or getattr(instrument, "ticker", None) or str(instrument)

    @staticmethod
    def _last_datetime(df: pd.DataFrame) -> pd.Timestamp | None:
        if df is None or df.empty:
            return None
        last = df["datetime"].iloc[-1]
        return pd.Timestamp(last)
