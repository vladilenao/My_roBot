import pandas as pd

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
        df["datetime"] = pd.date_range("2024-01-01", periods=len(df), freq="1h")
        ta = strategy.compute(df)
        decision = strategy.decide(ta)
        assert decision.signal_type is SignalType.BUY
        assert decision.idea_references == {
            "pattern_id": "ab_cd_0.2:long:2024-01-01T03:00:00:2024-01-01T06:00:00:2024-01-01T09:00:00:2024-01-01T12:00:00",
            "a": 80.0,
            "b": 91.5,
            "c": 84.0,
            "d": 117.214,
            "time_available": "2024-01-01T14:00:00",
        }
        assert decision.event_id == (
            "harmonic_abcd::ab_cd_0.2:long:2024-01-01T03:00:00:"
            "2024-01-01T06:00:00:2024-01-01T09:00:00:2024-01-01T12:00:00:BUY"
        )

    def test_pattern_references_are_unavailable_before_c_confirmation(self):
        strategy = HarmonicAbcdStrategy()
        df = bullish_series()
        df["datetime"] = pd.date_range("2024-01-01", periods=len(df), freq="1h")

        before_confirmation = strategy.compute(df.iloc[:14])
        confirmed = strategy.compute(df.iloc[:15])

        assert before_confirmation["harmonic_signal"].eq(0).all()
        assert before_confirmation["harmonic_pattern_id"].isna().all()
        assert confirmed["harmonic_pattern_id"].iloc[-1] == (
            "ab_cd_0.2:long:2024-01-01T03:00:00:2024-01-01T06:00:00:"
            "2024-01-01T09:00:00:2024-01-01T12:00:00"
        )
        assert confirmed["harmonic_time_available"].iloc[-1] == pd.Timestamp("2024-01-01T14:00:00")

    def test_future_bars_do_not_change_confirmed_pattern_event(self):
        strategy = HarmonicAbcdStrategy()
        df = bullish_series()
        df["datetime"] = pd.date_range("2024-01-01", periods=len(df), freq="1h")

        confirmed = strategy.compute(df.iloc[:15])
        with_future = df.copy()
        with_future.loc[15:, ["high", "low", "close"]] = [999.0, 1.0, 500.0]
        replayed = strategy.compute(with_future)

        event = strategy.decide(confirmed)
        replayed_event = strategy.decide(replayed.iloc[:15])
        assert replayed_event == event

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
        assert not hasattr(decision, "stop_loss")
        assert not hasattr(decision, "take_profit")
        assert not hasattr(decision, "trend_direction")
        assert decision.price > 0

    def test_price_beyond_d_gives_hold(self):
        # Формация валидна, но бар подтверждения закрывается выше цели D.
        df = bullish_series(x=103.0, a=80.0, b=91.5, c=84.0)
        df.loc[14, "close"] = 999.0
        df.loc[14, "high"] = 999.0
        df.loc[14, "low"] = 999.0
        ta = HarmonicAbcdStrategy().compute(df)
        assert ta["harmonic_signal"].iloc[14] == 0
