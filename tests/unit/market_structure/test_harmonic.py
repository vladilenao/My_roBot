import pandas as pd

from src.market_structure.harmonic import Direction, HarmonicPatternDetector

from tests.unit.market_structure.builders import bearish_series, bullish_series


class TestHarmonicPatternDetector:
    def test_detects_valid_bull_long(self):
        det = HarmonicPatternDetector()
        patterns = det.analyze(bullish_series())
        longs = [p for p in patterns if p.direction is Direction.LONG]
        assert any(p.c.index == 12 for p in longs)
        target = next(p.d_target for p in longs if p.c.index == 12)
        assert target == 80.0 + 1.618 * (103.0 - 80.0)

    def test_detects_valid_bear_short(self):
        det = HarmonicPatternDetector()
        patterns = det.analyze(bearish_series())
        shorts = [p for p in patterns if p.direction is Direction.SHORT]
        assert any(p.c.index == 12 for p in shorts)
        target = next(p.d_target for p in shorts if p.c.index == 12)
        assert target == 103.0 - 1.618 * (103.0 - 80.0)

    def test_b_below_min_retracement_invalidates(self):
        det = HarmonicPatternDetector()
        # B на ~21% ретрейсмента XA (ниже нижней границы 38.2%)
        patterns = det.analyze(bullish_series(b=84.8))
        assert not [p for p in patterns if p.direction is Direction.LONG and p.c.index == 12]

    def test_b_above_max_retracement_invalidates(self):
        det = HarmonicPatternDetector()
        # B на ~75% ретрейсмента XA (за верхней границей 61.8%)
        patterns = det.analyze(bullish_series(b=97.25))
        assert not [p for p in patterns if p.direction is Direction.LONG and p.c.index == 12]

    def test_c_below_min_retracement_invalidates(self):
        det = HarmonicPatternDetector()
        # C на ~81.6% ретрейсмента AB — за нижней границей 78.6% (глубже B-стороны)
        patterns = det.analyze(bullish_series(c=82.0))
        assert not [p for p in patterns if p.direction is Direction.LONG and p.c.index == 12]

    def test_c_above_max_retracement_invalidates(self):
        det = HarmonicPatternDetector()
        # C на ~35% ретрейсмента AB — за верхней границей 38.2% (ближе к B)
        patterns = det.analyze(bullish_series(c=88.5))
        assert not [p for p in patterns if p.direction is Direction.LONG and p.c.index == 12]

    def test_tolerance_relaxes_boundaries(self):
        # Строгий интервал C = [82.46, 87.11]; допустимое отклонение при
        # fib_tolerance=0.1 и амплитуде AB=11.5 -> запас ~1.15.
        det_strict = HarmonicPatternDetector()
        det_loose = HarmonicPatternDetector(fib_tolerance=0.1)
        df = bullish_series(c=88.2, tail=96.0)
        assert not [p for p in det_strict.analyze(df) if p.direction is Direction.LONG and p.c.index == 12]
        assert any(p.direction is Direction.LONG and p.c.index == 12 for p in det_loose.analyze(df))

    def test_history_too_short_returns_empty(self):
        det = HarmonicPatternDetector()
        assert det.analyze(bullish_series()[:7]) == []

    def test_empty_frame_returns_empty(self):
        det = HarmonicPatternDetector()
        assert det.analyze(pd.DataFrame()) == []

    def test_warmup_is_40(self):
        det = HarmonicPatternDetector()
        assert det.warmup == 40