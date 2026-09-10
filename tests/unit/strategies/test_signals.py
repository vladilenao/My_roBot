import pandas as pd
import pytest

from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.macd import MacdIndicator, MacdSignalEnum
from src.strategies.indicators.rsi import RsiIndicator, RsiSignalEnum
from src.strategies.indicators.stochastic import StochasticIndicator, StochasticSignalEnum
from src.strategies.macd_rsi_stoch_strategy import MacdRsiStochStrategy, DEFAULT_CONFIG
from src.strategies.signals import get_last_signals


def _make_ta(macd_values, rsi_values, stoch_values):
    n = len(macd_values)
    return pd.DataFrame({
        "macd_signal": macd_values,
        "rsi_signal": rsi_values,
        "stoch_signal": stoch_values,
        "close": [100.0] * n,
    })


class TestDecide:
    def setup_method(self):
        self.strategy = MacdRsiStochStrategy(config=DEFAULT_CONFIG)

    def test_buy_when_consensus_positive(self):
        ta = _make_ta([1] * 5, [1] * 5, [1] * 5)
        decision = self.strategy.decide(ta)
        assert decision.signal_type is SignalType.BUY
        assert decision.price == 100.0

    def test_sell_when_consensus_negative(self):
        ta = _make_ta([-1] * 5, [-1] * 5, [-1] * 5)
        decision = self.strategy.decide(ta)
        assert decision.signal_type is SignalType.SELL
        assert decision.price == 100.0

    def test_hold_on_mixed_signals(self):
        ta = _make_ta([1] * 5, [1] * 5, [-1] * 5)
        decision = self.strategy.decide(ta)
        assert decision.signal_type is SignalType.HOLD

    def test_hold_on_zero_signals(self):
        ta = _make_ta([0] * 5, [0] * 5, [0] * 5)
        decision = self.strategy.decide(ta)
        assert decision.signal_type is SignalType.HOLD

    def test_decide_uses_only_last_candle_window(self):
        ta = _make_ta([1, 1, -1, -1, -1], [1, 1, -1, -1, -1], [1, 1, -1, -1, -1])
        decision = self.strategy.decide(ta)
        assert decision.signal_type is SignalType.SELL

    def test_decision_is_frozen_dataclass(self):
        ta = _make_ta([1] * 5, [1] * 5, [1] * 5)
        decision = self.strategy.decide(ta)
        assert isinstance(decision, Decision)
        with pytest.raises(Exception):
            decision.price = 0.0


class TestStrategyContract:
    def test_name_and_window(self):
        strategy = MacdRsiStochStrategy(config=DEFAULT_CONFIG)
        assert strategy.NAME == "macd_rsi_stoch"
        assert strategy.STRATEGY_WINDOW == 5

    def test_registered_in_registry(self):
        from src.strategies import get_strategy

        assert isinstance(get_strategy("macd_rsi_stoch", config=DEFAULT_CONFIG), MacdRsiStochStrategy)

    def test_required_history(self):
        strategy = MacdRsiStochStrategy(config=DEFAULT_CONFIG)
        assert strategy.required_history() == strategy.STRATEGY_WINDOW + 35


class TestGetLastSignals:
    def _make_df(self, macd_signals, rsi_signals, stoch_signals):
        return pd.DataFrame({
            'macd_signal': macd_signals,
            'rsi_signal': rsi_signals,
            'stoch_signal': stoch_signals,
        })

    def test_basic_window(self):
        df = self._make_df([1, 1, 1, 1, 1], [0, 0, 0, 0, 0], [-1, -1, -1, -1, -1])
        result = get_last_signals(df, window=3, signal_columns=["macd_signal", "rsi_signal", "stoch_signal"])
        assert result == [3, 0, -3]

    def test_window_larger_than_data(self):
        df = self._make_df([1, 1], [0, 0], [-1, -1])
        result = get_last_signals(df, window=10, signal_columns=["macd_signal", "rsi_signal", "stoch_signal"])
        assert result == [2, 0, -2]

    def test_window_equals_data(self):
        df = self._make_df([1, 2, 3], [4, 5, 6], [7, 8, 9])
        result = get_last_signals(df, window=3, signal_columns=["macd_signal", "rsi_signal", "stoch_signal"])
        assert result == [6, 15, 24]

    def test_subset_of_columns(self):
        df = self._make_df([1] * 10, [0] * 10, [-1] * 10)
        result = get_last_signals(df, window=5, signal_columns=["macd_signal"])
        assert result == [5]


class TestBaseSignalEnum:
    def test_no_signal_is_zero_in_all_enums(self):
        assert MacdSignalEnum.NO_SIGNAL == 0
        assert RsiSignalEnum.NO_SIGNAL == 0
        assert StochasticSignalEnum.NO_SIGNAL == 0

    def test_all_signal_enums_are_int_enums(self):
        assert issubclass(MacdSignalEnum, int)
        assert issubclass(RsiSignalEnum, int)
        assert issubclass(StochasticSignalEnum, int)

    def test_all_signal_enums_inherit_base(self):
        from src.strategies.indicators.base import BaseSignalEnum
        assert issubclass(MacdSignalEnum, BaseSignalEnum)
        assert issubclass(RsiSignalEnum, BaseSignalEnum)
        assert issubclass(StochasticSignalEnum, BaseSignalEnum)


class TestIndicatorSignalEnum:
    def test_macd_returns_macd_signal_enum(self):
        indicator = MacdIndicator(fast=12, slow=26, signal=9)
        assert indicator.signal_enum is MacdSignalEnum

    def test_rsi_returns_rsi_signal_enum(self):
        indicator = RsiIndicator(period=14)
        assert indicator.signal_enum is RsiSignalEnum

    def test_stochastic_returns_stochastic_signal_enum(self):
        indicator = StochasticIndicator(k=14, d=3, smooth_k=3)
        assert indicator.signal_enum is StochasticSignalEnum

    def test_macd_enum_contains_all_members(self):
        indicator = MacdIndicator(fast=12, slow=26, signal=9)
        members = list(indicator.signal_enum)
        assert len(members) == 3
        assert MacdSignalEnum.NO_SIGNAL in members
        assert MacdSignalEnum.BULLISH_CROSSOVER_BELOW_ZERO in members
        assert MacdSignalEnum.BEARISH_CROSSOVER_ABOVE_ZERO in members

    def test_rsi_enum_contains_all_members(self):
        indicator = RsiIndicator(period=14)
        members = list(indicator.signal_enum)
        assert len(members) == 3
        assert RsiSignalEnum.NO_SIGNAL in members
        assert RsiSignalEnum.CROSS_ABOVE_50 in members
        assert RsiSignalEnum.CROSS_BELOW_50 in members

    def test_stochastic_enum_contains_all_members(self):
        indicator = StochasticIndicator(k=14, d=3, smooth_k=3)
        members = list(indicator.signal_enum)
        assert len(members) == 3
        assert StochasticSignalEnum.NO_SIGNAL in members
        assert StochasticSignalEnum.EXIT_OVERSOLD in members
        assert StochasticSignalEnum.EXIT_OVERBOUGHT in members

    def test_macd_signal_enum_validates_value(self):
        indicator = MacdIndicator(fast=12, slow=26, signal=9)
        assert indicator.signal_enum(1) == MacdSignalEnum.BULLISH_CROSSOVER_BELOW_ZERO
        assert indicator.signal_enum(-1) == MacdSignalEnum.BEARISH_CROSSOVER_ABOVE_ZERO
        assert indicator.signal_enum(0) == MacdSignalEnum.NO_SIGNAL
        with pytest.raises(ValueError):
            indicator.signal_enum(99)

    def test_rsi_signal_enum_validates_value(self):
        indicator = RsiIndicator(period=14)
        assert indicator.signal_enum(1) == RsiSignalEnum.CROSS_ABOVE_50
        assert indicator.signal_enum(-1) == RsiSignalEnum.CROSS_BELOW_50
        with pytest.raises(ValueError):
            indicator.signal_enum(99)

    def test_stochastic_signal_enum_validates_value(self):
        indicator = StochasticIndicator(k=14, d=3, smooth_k=3)
        assert indicator.signal_enum(1) == StochasticSignalEnum.EXIT_OVERSOLD
        assert indicator.signal_enum(-1) == StochasticSignalEnum.EXIT_OVERBOUGHT
        with pytest.raises(ValueError):
            indicator.signal_enum(99)
