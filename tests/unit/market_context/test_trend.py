import numpy as np
import pandas as pd
import pytest

from src.market_context.models import TrendDirection
from src.market_context.trend import TrendAnalyzer


def _uptrend_frames(n=80):
    close = np.linspace(100.0, 130.0, n)
    return pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + 0.5,
            "low": close - 0.8,
            "close": close,
            "volume": 1000,
        }
    )


def _downtrend_frames(n=80):
    close = np.linspace(130.0, 100.0, n)
    return pd.DataFrame(
        {
            "open": close + 0.3,
            "high": close + 0.8,
            "low": close - 0.5,
            "close": close,
            "volume": 1000,
        }
    )


def _flat_frames(n=80):
    close = np.full(n, 100.0)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000,
        }
    )


class TestTrendAnalyzer:
    def test_upward_trend(self):
        analyzer = TrendAnalyzer()
        result = analyzer.analyze(_uptrend_frames())
        assert result.direction == TrendDirection.UP
        assert result.strength > 0.0

    def test_downward_trend(self):
        analyzer = TrendAnalyzer()
        result = analyzer.analyze(_downtrend_frames())
        assert result.direction == TrendDirection.DOWN
        assert result.strength > 0.0

    def test_flat_trend(self):
        analyzer = TrendAnalyzer()
        result = analyzer.analyze(_flat_frames())
        assert result.direction == TrendDirection.FLAT
        assert result.strength == 0.0

    def test_insufficient_history_no_error(self):
        analyzer = TrendAnalyzer(ema_short=9, ema_long=21, adx_period=14)
        df = _uptrend_frames(n=10)
        result = analyzer.analyze(df)
        assert result.direction is not None

    def test_empty_dataframe(self):
        analyzer = TrendAnalyzer()
        result = analyzer.analyze(pd.DataFrame())
        assert result.direction == TrendDirection.FLAT
        assert result.strength == 0.0

    def test_flat_when_adx_low_regardless_of_ema(self):
        analyzer = TrendAnalyzer(ema_short=2, ema_long=3, adx_period=14)
        df = _flat_frames()
        result = analyzer.analyze(df)
        assert result.direction == TrendDirection.FLAT
        assert result.strength == 0.0

    def test_invalid_params(self):
        with pytest.raises(ValueError):
            TrendAnalyzer(ema_short=0)
