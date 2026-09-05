import pandas as pd
import pytest

from src.market_structure.swings import SwingDetector, SwingKind, SwingPoint
from tests.unit.market_structure.builders import bearish_series


class TestSwingDetector:
    def test_detects_swing_high_and_low(self):
        total = 16
        df = pd.DataFrame(
            {
                "open": [100.0] * total,
                "high": [96.0] * total,
                "low": [92.0] * total,
                "close": [94.0] * total,
            }
        )
        df.at[3, "high"] = 102.0
        df.at[3, "low"] = 99.0
        df.at[6, "low"] = 79.0
        df.at[6, "high"] = 90.0
        df.at[9, "high"] = 91.0
        df.at[12, "low"] = 84.0
        detector = SwingDetector()
        points = detector.detect(df)
        highs = [p for p in points if p.kind is SwingKind.HIGH]
        lows = [p for p in points if p.kind is SwingKind.LOW]
        assert any(p.index == 3 and p.price == 102.0 for p in highs)
        assert any(p.index == 6 and p.price == 79.0 for p in lows)

    def test_empty_on_null_frame(self):
        detector = SwingDetector()
        assert detector.detect(None) == []

    def test_short_frame_returns_empty_no_error(self):
        detector = SwingDetector()
        df = pd.DataFrame(
            {
                "open": [100.0] * 3,
                "high": [100.0] * 3,
                "low": [100.0] * 3,
                "close": [100.0] * 3,
            }
        )
        assert detector.detect(df) == []

    def test_points_sorted_by_index(self):
        detector = SwingDetector()
        df = bearish_series()
        points = detector.detect(df)
        indexes = [p.index for p in points]
        assert len(points) >= 4
        assert all(a < b for a, b in zip(indexes, indexes[1:]))

    def test_returns_swingpoint_instances(self):
        detector = SwingDetector()
        n = 20
        highs = [96.0] * n
        highs[5] = 101.0
        df = pd.DataFrame(
            {
                "open": [94.0] * n,
                "high": highs,
                "low": [92.0] * n,
                "close": [94.0] * n,
            }
        )
        points = detector.detect(df)
        assert all(isinstance(p, SwingPoint) for p in points)

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            SwingDetector(left=0)
        with pytest.raises(ValueError):
            SwingDetector(right=0)