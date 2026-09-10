import pandas as pd
import pytest

from src.strategies import get_strategy
from src.strategies.contracts import SignalType
from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG, HarmonicAbcdStrategy
from src.strategies.registry import strategy_names
from tests.unit.market_structure.builders import bearish_series, bullish_series


class TestHarmonicAbcdStrategy:
    def test_registered_and_discoverable(self):
        assert "harmonic_abcd" in strategy_names()
        strategy = get_strategy("harmonic_abcd", config=DEFAULT_CONFIG)
        assert isinstance(strategy, HarmonicAbcdStrategy)

    def test_bull_signal_on_confirmation_bar(self):
        df = bullish_series()
        ta = HarmonicAbcdStrategy().compute(df)
        assert ta["harmonic_signal"].iloc[12] == 0  # свинг ещё не подтверждён
        assert ta["harmonic_signal"].iloc[14] == 1  # бар подтверждения формации
        assert ta["harmonic_signal"].iloc[15] == 0  # повторная формация — не событие

    def test_bear_signal_on_confirmation_bar(self):
        df = bearish_series()
        ta = HarmonicAbcdStrategy().compute(df)
        assert ta["harmonic_signal"].iloc[14] == -1

    def test_hold_without_formation(self):
        strategy = HarmonicAbcdStrategy()
        ta = strategy.compute(pd.DataFrame())
        assert ta.empty or ta["harmonic_signal"].eq(0).all()

    def test_hold_on_flat_data(self):
        df = pd.DataFrame(
            {
                "open": [100.0] * 20,
                "high": [100.0] * 20,
                "low": [100.0] * 20,
                "close": [100.0] * 20,
            }
        )
        ta = HarmonicAbcdStrategy().compute(df)
        assert ta["harmonic_signal"].eq(0).all()

    def test_decide_buy(self):
        strategy = HarmonicAbcdStrategy()
        # последняя закрытая свеча — бар подтверждения формации (индекс 14)
        df = bullish_series()[:15]
        ta = strategy.compute(df)
        decision = strategy.decide(ta)
        assert decision.signal_type is SignalType.BUY

    def test_decide_hold(self):
        strategy = HarmonicAbcdStrategy()
        ta = strategy.compute(
            pd.DataFrame(
                {
                    "open": [100.0] * 20,
                    "high": [100.0] * 20,
                    "low": [100.0] * 20,
                    "close": [100.0] * 20,
                }
            )
        )
        decision = strategy.decide(ta)
        assert decision.signal_type is SignalType.HOLD

    def test_required_history_matches_detector_warmup(self):
        assert HarmonicAbcdStrategy().required_history() == 40

    def test_no_tp_sl_trend_in_decision(self):
        strategy = HarmonicAbcdStrategy()
        df = bullish_series()[:15]
        ta = strategy.compute(df)
        decision = strategy.decide(ta)
        assert decision.stop_loss is None
        assert decision.take_profit is None
        assert decision.trend_direction is None
        assert decision.price > 0

    def test_price_beyond_d_gives_hold(self):
        # Формация валидна, но бар подтверждения закрывается выше цели D.
        df = bullish_series(x=103.0, a=80.0, b=91.5, c=84.0)
        df.loc[14, "close"] = 999.0
        df.loc[14, "high"] = 999.0
        df.loc[14, "low"] = 999.0
        ta = HarmonicAbcdStrategy().compute(df)
        assert ta["harmonic_signal"].iloc[14] == 0