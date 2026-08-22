import pandas as pd
import pytest

from src.strategies.base import Decision, SignalType
from src.strategies.macd_rsi_stoch.signals.aggregate import get_last_signals
from src.strategies.macd_rsi_stoch.strategy import MacdRsiStochStrategy


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
        self.strategy = MacdRsiStochStrategy()

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
        strategy = MacdRsiStochStrategy()
        assert strategy.NAME == "macd_rsi_stoch"
        assert strategy.STRATEGY_WINDOW == 5

    def test_registered_in_registry(self):
        from src.strategies import get_strategy

        assert isinstance(get_strategy("macd_rsi_stoch"), MacdRsiStochStrategy)

    def test_required_history(self):
        strategy = MacdRsiStochStrategy()
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
        macd, rsi, stoch = get_last_signals(df, window=3)
        assert macd == 3
        assert rsi == 0
        assert stoch == -3

    def test_window_larger_than_data(self):
        df = self._make_df([1, 1], [0, 0], [-1, -1])
        macd, rsi, stoch = get_last_signals(df, window=10)
        assert macd == 2
        assert rsi == 0
        assert stoch == -2

    def test_window_equals_data(self):
        df = self._make_df([1, 2, 3], [4, 5, 6], [7, 8, 9])
        macd, rsi, stoch = get_last_signals(df, window=3)
        assert macd == 6
        assert rsi == 15
        assert stoch == 24

    def test_default_window(self):
        df = self._make_df([1] * 10, [0] * 10, [-1] * 10)
        macd, rsi, stoch = get_last_signals(df)
        assert macd == 5
        assert rsi == 0
        assert stoch == -5
