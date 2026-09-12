import pandas as pd

from src.market_context.context_cache import MarketContextCache
from src.market_context.sr_levels import SRLevelsCalculator
from src.market_context.trend import TrendAnalyzer


class _Inst:
    def __init__(self, ticker="BR", base_code="BR"):
        self.ticker = ticker
        self.base_code = base_code


def _frames(n=20, base=100.0):
    close = pd.Series(range(n), dtype=float) + base
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2024-01-01", periods=n, freq="1h"),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000,
        }
    )


class FakeDataCache:
    def __init__(self, frames):
        self.frames = frames

    def frame_for(self, instrument, timeframe):
        return self.frames[(instrument.base_code, timeframe)]


class CountingTrend(TrendAnalyzer):
    calls = 0

    def analyze(self, df):
        self.calls += 1
        return super().analyze(df)


class TestMarketContextCache:
    def test_first_request_computes(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(FakeDataCache({("BR", "1h"): _frames()}), trend, calc)
        ctx = cache.get_context(inst, "1h")
        assert ctx.trend.direction is not None
        assert trend.calls == 1

    def test_second_request_cached_without_recompute(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(FakeDataCache({("BR", "1h"): _frames()}), trend, calc)
        ctx1 = cache.get_context(inst, "1h")
        ctx2 = cache.get_context(inst, "1h")
        assert ctx1 is ctx2
        assert trend.calls == 1

    def test_new_candle_recomputes(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        df1 = _frames(n=20)
        cache = MarketContextCache(FakeDataCache({("BR", "1h"): df1}), trend, calc)
        cache.get_context(inst, "1h")
        assert trend.calls == 1

        df2 = _frames(n=21)
        cache._data_cache.frames[("BR", "1h")] = df2
        cache.get_context(inst, "1h")
        assert trend.calls == 2

    def test_same_candle_does_not_recompute(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        df = _frames(n=20)
        cache = MarketContextCache(FakeDataCache({("BR", "1h"): df}), trend, calc)
        cache.get_context(inst, "1h")
        cache.get_context(inst, "1h")
        assert trend.calls == 1

    def test_empty_data_returns_empty_context(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(FakeDataCache({("BR", "1h"): pd.DataFrame()}), trend, calc)
        ctx = cache.get_context(inst, "1h")
        assert ctx.sr_levels == []


class TestMultiTimeframeContext:
    def test_contexts_of_different_timeframes_are_isolated(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(
            FakeDataCache({("BR", "1h"): _frames(), ("BR", "15m"): _frames(n=30)}),
            trend, calc,
        )

        ctx_1h = cache.get_context(inst, "1h")
        ctx_15m = cache.get_context(inst, "15m")

        assert ctx_1h is not ctx_15m
        assert trend.calls == 2

        # повторные запросы обеих пар — из кэша, без пересчёта
        cache.get_context(inst, "1h")
        cache.get_context(inst, "15m")
        assert trend.calls == 2
