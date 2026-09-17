import numpy as np
import pandas as pd

from src.portfolio.models import Position
from src.strategies.contracts import SignalType
from src.strategies.ma_cloud_rsi_macd_strategy import DEFAULT_CONFIG, MaCloudRsiMacdStrategy, MaCloudState


def _frame(close: np.ndarray) -> pd.DataFrame:
    start = pd.Timestamp("2024-01-01")
    return pd.DataFrame({
        "datetime": [start + pd.Timedelta(days=i) for i in range(len(close))],
        "open": close,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": [1000] * len(close),
    })


def _ta(ma_signal=0, rsi_signal=0, macd_signal=0) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=5, freq="1h"),
        "high": [120.0] * 5,
        "low": [116.0] * 5,
        "close": [118.0] * 5,
        "sma_fast": [120.0] * 5,
        "sma_slow": [115.0] * 5,
        "ma_cloud_signal": [ma_signal] * 5,
        "rsi": [55.0] * 5,
        "rsi_signal": [rsi_signal] * 5,
        "macd_zero_signal": [macd_signal] * 5,
    })


def _confirmation_history() -> pd.DataFrame:
    ta = _ta()
    ta.loc[2, "rsi_signal"] = 1
    ta.loc[3, "macd_zero_signal"] = 1
    ta.loc[4, "ma_cloud_signal"] = 1
    return ta


def _warmed_confirmation_history() -> pd.DataFrame:
    ta = pd.concat([_ta()] * 9, ignore_index=True).iloc[:41].copy()
    ta["datetime"] = pd.date_range("2024-01-01", periods=len(ta), freq="1h")
    ta.loc[38, "rsi_signal"] = 1
    ta.loc[39, "macd_zero_signal"] = 1
    ta.loc[40, "ma_cloud_signal"] = 1
    return ta


class TestMaCloudRsiMacdStrategy:
    def test_required_history(self):
        assert MaCloudRsiMacdStrategy(DEFAULT_CONFIG).required_history() == 41

    def test_compute_adds_signal_columns(self):
        ta = MaCloudRsiMacdStrategy().compute(_frame(np.linspace(100, 120, 60)))
        assert {"ma_cloud_signal", "rsi_signal", "macd_zero_signal"} <= set(ta.columns)

    def test_third_distinct_confirmation_emits_entry_event_at_high(self):
        strategy = MaCloudRsiMacdStrategy()
        state = MaCloudState(pending_long=(("rsi", 2), ("macd_zero", 3)))
        state, decision = strategy._step(state, 4, _ta(ma_signal=1).iloc[-1], "1h")

        assert decision.signal_type is SignalType.BUY
        assert decision.price == 120.0
        assert decision.idea_references == {"entry_reference": "high"}
        assert decision.event_id == "ma_cloud_rsi_macd:1h:2024-01-01T04:00:00:BUY"
        assert decision.available_at == decision.bar_time
        assert state == MaCloudState()

    def test_third_distinct_confirmation_emits_short_entry_at_low(self):
        strategy = MaCloudRsiMacdStrategy()
        state = MaCloudState(pending_short=(("rsi", 2), ("macd_zero", 3)))
        _, decision = strategy._step(state, 4, _ta(ma_signal=-1).iloc[-1], "1h")

        assert decision.signal_type is SignalType.SELL
        assert decision.price == 116.0
        assert decision.idea_references == {"entry_reference": "low"}

    def test_confirmation_requires_three_distinct_indicators_within_six_bars(self):
        strategy = MaCloudRsiMacdStrategy()
        state = MaCloudState(pending_long=(("rsi", 0), ("macd_zero", 1)))

        state, decision = strategy._step(state, 6, _ta(rsi_signal=1).iloc[-1])

        assert decision.signal_type is SignalType.HOLD
        assert state.pending_long == (("macd_zero", 1), ("rsi", 6))

    def test_repeat_calls_are_deterministic_and_do_not_track_positions(self):
        strategy = MaCloudRsiMacdStrategy()
        ta = _confirmation_history()

        assert strategy.decide(ta) == strategy.decide(ta)
        state = strategy._replay_state(ta)
        assert not hasattr(state, "long_contracts")
        assert not hasattr(state, "short_contracts")

    def test_shared_instance_returns_the_same_event_to_two_profiles(self):
        strategy = MaCloudRsiMacdStrategy()
        ta = _confirmation_history()

        ma_cloud_profile_event = strategy.decide(ta, "1h")
        levels_rr_profile_event = strategy.decide(ta, "1h")

        assert ma_cloud_profile_event == levels_rr_profile_event
        assert ma_cloud_profile_event.signal_type is SignalType.BUY
        assert ma_cloud_profile_event.price == 120.0

    def test_entry_event_is_independent_of_portfolio_position_state(self):
        strategy = MaCloudRsiMacdStrategy()
        ta = _confirmation_history()
        before = strategy.decide(ta, "1h")
        position = Position("trade-1", "NG", "BUY", 1, 100.0, None, None, None)
        position.apply_fill(110.0, 2)
        position.reduce(1)

        assert strategy.decide(ta, "1h") == before

    def test_expected_events_are_entry_only(self):
        strategy = MaCloudRsiMacdStrategy()
        ta = _warmed_confirmation_history()

        first = strategy.expected_events(ta)
        strategy.decide(ta)
        second = strategy.expected_events(ta)

        assert list(first.columns) == ["datetime", "signal", "price"]
        assert first.iloc[0].to_dict() == {
            "datetime": pd.Timestamp("2024-01-02 16:00:00"),
            "signal": "BUY",
            "price": 120.0,
        }
        pd.testing.assert_frame_equal(first, second)
