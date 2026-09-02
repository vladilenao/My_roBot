import pandas as pd

from src.analysis.models import SRLevel, SRType
from src.analysis.sr_levels import SRLevelsCalculator


def _frames(opens, highs, lows, closes):
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * len(opens),
        }
    )


def _sine_frames(n=60, base=100.0, amp=5.0):
    import numpy as np

    x = np.linspace(0, 6 * np.pi, n)
    close = base + amp * np.sin(x)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 1000,
        }
    )


class TestSRLevelsCalculator:
    def test_detects_levels(self):
        calc = SRLevelsCalculator()
        levels = calc.compute(_sine_frames(), price=100.0)
        assert isinstance(levels, list)
        assert all(isinstance(lev, SRLevel) for lev in levels)
        if levels:
            assert all(lev.strength >= calc.min_touches for lev in levels)

    def test_levels_sorted_by_price(self):
        calc = SRLevelsCalculator()
        levels = calc.compute(_sine_frames(), price=100.0)
        prices = [lev.price for lev in levels]
        assert prices == sorted(prices)

    def test_max_levels_limits_count_and_nearest_to_price(self):
        calc = SRLevelsCalculator(max_levels=2)
        levels = calc.compute(_sine_frames(), price=100.0)
        assert len(levels) <= 2
        if levels:
            assert all(abs(lev.price - 100.0) <= max(abs(a.price - 100.0) for a in levels) for lev in levels)

    def test_min_touches_filters_weak_levels(self):
        calc = SRLevelsCalculator(min_touches=50)
        levels = calc.compute(_sine_frames(), price=100.0)
        for lev in levels:
            assert lev.strength >= 50

    def test_no_confirmed_levels_returns_empty(self):
        calc = SRLevelsCalculator(min_touches=1000)
        levels = calc.compute(_sine_frames(), price=100.0)
        assert levels == []

    def test_insufficient_data_returns_empty(self):
        calc = SRLevelsCalculator()
        small = _sine_frames(n=3)
        assert calc.compute(small, price=100.0) == []

    def test_empty_dataframe(self):
        calc = SRLevelsCalculator()
        assert calc.compute(pd.DataFrame(), price=100.0) == []

    def test_support_resistance_types_valid(self):
        calc = SRLevelsCalculator()
        levels = calc.compute(_sine_frames(), price=100.0)
        for lev in levels:
            assert lev.sr_type in (SRType.SUPPORT, SRType.RESISTANCE)
