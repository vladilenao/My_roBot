import pandas as pd

from src.analysis.context_cache import MarketContextCache
from src.analysis.sr_levels import SRLevelsCalculator
from src.analysis.trend import TrendAnalyzer


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

    def frame_for(self, instrument):
        return self.frames[instrument.base_code]


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
        cache = MarketContextCache(FakeDataCache({"BR": _frames()}), trend, calc)
        ctx = cache.get_context(inst)
        assert ctx.trend.direction is not None
        assert trend.calls == 1

    def test_second_request_cached_without_recompute(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(FakeDataCache({"BR": _frames()}), trend, calc)
        ctx1 = cache.get_context(inst)
        ctx2 = cache.get_context(inst)
        assert ctx1 is ctx2
        assert trend.calls == 1

    def test_new_candle_recomputes(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        df1 = _frames(n=20)
        cache = MarketContextCache(FakeDataCache({"BR": df1}), trend, calc)
        cache.get_context(inst)
        assert trend.calls == 1

        df2 = _frames(n=21)
        cache._data_cache.frames["BR"] = df2
        cache.get_context(inst)
        assert trend.calls == 2

    def test_same_candle_does_not_recompute(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        df = _frames(n=20)
        cache = MarketContextCache(FakeDataCache({"BR": df}), trend, calc)
        cache.get_context(inst)
        cache.get_context(inst)
        assert trend.calls == 1

    def test_empty_data_returns_empty_context(self):
        trend = CountingTrend()
        calc = SRLevelsCalculator()
        inst = _Inst()
        cache = MarketContextCache(FakeDataCache({"BR": pd.DataFrame()}), trend, calc)
        ctx = cache.get_context(inst)
        assert ctx.sr_levels == []
