from unittest.mock import MagicMock
from datetime import timedelta

import pandas as pd
import pytest

from src.bot import TradingBot
from src.instruments import Instrument
from src.strategies.contracts import Decision, SignalType


def _inst(*args):
    return Instrument(*args) if len(args) == 3 else Instrument(args[0], args[0], args[1])


def _df():
    return pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=10, freq="1h"),
        "open": [100.0] * 10,
        "high": [101.0] * 10,
        "low": [99.0] * 10,
        "close": [100.5] * 10,
        "volume": [1000] * 10,
    })


def _make_strategy(name="macd_rsi_stoch", decision=None):
    strat = MagicMock()
    strat.NAME = name
    strat.compute.return_value = _df()
    strat.decide.return_value = decision or Decision(SignalType.BUY, 100.5)
    return strat


class FakeTimeline:
    def __init__(self, ticks=1, fallback=1.0, timeframe="1h"):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframe = timeframe
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        bar_ready()

    def bar_close(self, bar_start):
        return bar_start + timedelta(hours=1)

    def fallback_secs(self):
        return self.fallback


class FakeCache:
    def __init__(self, frames=None, refresh_error=None):
        self.frames = frames or {}
        self.refresh_error = refresh_error
        self.refresh_calls = 0
        self.refresh_forces = []

    def refresh_if_new_candle(self, force=False):
        self.refresh_calls += 1
        self.refresh_forces.append(force)
        if self.refresh_error:
            raise self.refresh_error

    def has_fresh_closed_bar(self, now=None):
        return True

    def frame_for(self, instrument):
        return self.frames.get(instrument.ticker, pd.DataFrame())


class RecordingExecution:
    def __init__(self):
        self.decisions = []

    def execute(self, decision, instrument):
        self.decisions.append((decision, instrument))


class PollingTimeline:
    def __init__(self, ticks=1, fallback=1.0, timeframe="1h"):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframe = timeframe
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        while not bar_ready():
            pass

    def bar_close(self, bar_start):
        return bar_start + timedelta(hours=1)

    def fallback_secs(self):
        return self.fallback


class TimeoutTimeline:
    def __init__(self, ticks=1, fallback=1.0, timeframe="1h"):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframe = timeframe
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        bar_ready()

    def bar_close(self, bar_start):
        return bar_start + timedelta(hours=1)

    def fallback_secs(self):
        return self.fallback


class LateBarCache(FakeCache):
    def __init__(self, publish_after, frames=None):
        super().__init__(frames=frames)
        self.publish_after = publish_after
        self.checks = 0

    def has_fresh_closed_bar(self, now=None):
        self.checks += 1
        return self.checks >= self.publish_after


class NeverPublishCache(FakeCache):
    def has_fresh_closed_bar(self, now=None):
        return False


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


def _make_bot(timeline, cache, execution, notifier, strategy, share=None, future=None, factory=None, heartbeat=None):
    return TradingBot(
        instruments=[],  # replace below
        notifier=notifier,
        strategy_map={"macd_rsi_stoch": object(), "flat_triangle": object()},
        data_cache=cache,
        timeline=timeline,
        execution=execution,
        strategy_factory=factory or (lambda name, config: strategy),
        share_strategies=share or {},
        future_strategies=future or {},
        heartbeat_every_ticks=heartbeat,
    )


class TestTradingBot:
    def test_fail_fast_on_unknown_strategy(self):
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": ["no_such_strategy"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        with pytest.raises(ValueError, match="no_such_strategy"):
            bot.run()

        assert bot._data_cache.refresh_calls == 0

    def test_future_uses_base_code(self):
        strategy = _make_strategy()
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"NGU6": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            future={"NG": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("NG (Природный газ) — NG-9.26", "NGU6", "future")]

        bot.run()

        assert len(execution.decisions) == 1
        label = bot._instruments[0].label
        assert execution.decisions[0][1].label == label
        assert strategy.decide.call_args.kwargs["timeframe"] == "1h"

    def test_share_uses_exact_ticker(self):
        strategy = _make_strategy()
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 1

    def test_delivers_signal_on_every_tick(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=2),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2

    def test_delivers_on_signal_change(self):
        strategy = MagicMock()
        strategy.compute.return_value = _df()
        strategy.decide.side_effect = [
            Decision(SignalType.HOLD, 100.5),
            Decision(SignalType.BUY, 100.5),
        ]
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=2),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2

    def test_delivers_on_price_change_with_same_signal(self):
        strategy = MagicMock()
        strategy.compute.return_value = _df()
        strategy.decide.side_effect = [
            Decision(SignalType.HOLD, 100.5),
            Decision(SignalType.HOLD, 101.0),
        ]
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=2),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2

    def test_strategy_failure_does_not_block_next(self):
        failing = _make_strategy()
        failing.compute.side_effect = Exception("boom")
        working = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(side_effect=[failing, working])
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"MUZ6": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=failing,
            future={"MU": ["macd_rsi_stoch", "flat_triangle"]},
            factory=factory,
        )
        bot._instruments = [_inst("MU (base) — MUZ6", "MUZ6", "future")]

        bot.run()

        assert working.compute.called is True

    def test_tick_error_notifies_trader(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}, refresh_error=RuntimeError("boom")),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert any("Ошибка робота" in m for m in bot._notifier.messages)

    def test_heartbeat_every_n_ticks(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=5),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": ["macd_rsi_stoch"]},
            heartbeat=2,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        heartbeats = [m for m in bot._notifier.messages if "Сердцебиение" in m]
        assert len(heartbeats) == 2
        assert "тиков работы — 4" in heartbeats[-1]

    def test_heartbeat_not_on_every_tick_when_interval_large(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=4),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": ["macd_rsi_stoch"]},
            heartbeat=5,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        heartbeats = [m for m in bot._notifier.messages if "Сердцебиение" in m]
        assert heartbeats == []

    def test_heartbeat_includes_error_count_and_resets(self):
        class FailOnceCache:
            def __init__(self):
                self.calls = 0

            def refresh_if_new_candle(self, force=False):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")

            def has_fresh_closed_bar(self, now=None):
                return True

            def frame_for(self, instrument):
                return _df()

        bot = _make_bot(
            timeline=FakeTimeline(ticks=2),
            cache=FailOnceCache(),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": ["macd_rsi_stoch"]},
            heartbeat=1,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        heartbeats = [m for m in bot._notifier.messages if "Сердцебиение" in m]
        assert len(heartbeats) == 1
        assert "ошибок за период — 1" in heartbeats[-1]

    def test_tick_held_until_fresh_bar_published(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = LateBarCache(publish_after=3, frames={"SBER": _df()})
        bot = _make_bot(
            timeline=PollingTimeline(ticks=1),
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert cache.checks >= 3
        assert len(execution.decisions) == 1

    def test_processes_with_available_data_on_timeout(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = NeverPublishCache(frames={"SBER": _df()})
        bot = _make_bot(
            timeline=TimeoutTimeline(ticks=1),
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 1

    def test_bootstrap_first_tick_runs_immediately_mid_period(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        timeline = FakeTimeline(ticks=1)
        bot = _make_bot(
            timeline=timeline,
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 1
        assert timeline.wait_boundaries == [False, True]

    def test_bootstrap_mid_period_does_not_force_refresh(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = FakeCache(frames={"SBER": _df()})
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 1
        assert not any(cache.refresh_forces)

    def test_decision_bar_time_is_candle_close(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        decision = execution.decisions[0][0]
        assert decision.bar_time == pd.Timestamp("2024-01-01 10:00")

    def test_bootstrap_waits_for_fresh_bar_on_boundary_launch(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = LateBarCache(publish_after=3, frames={"SBER": _df()})
        timeline = PollingTimeline(ticks=1)
        bot = _make_bot(
            timeline=timeline,
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert cache.checks >= 3
        assert len(execution.decisions) == 1
        assert timeline.wait_boundaries == [False, True]

    def test_bootstrap_retries_after_error_then_aligns_to_boundary(self):
        class FailOnceRefresh:
            def __init__(self):
                self.calls = 0
                self.ready = False

            def refresh_if_new_candle(self, force=False):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                self.ready = True

            def has_fresh_closed_bar(self, now=None):
                return self.ready

            def frame_for(self, instrument):
                return _df()

        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        timeline = FakeTimeline(ticks=3)
        bot = _make_bot(
            timeline=timeline,
            cache=FailOnceRefresh(),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": ["macd_rsi_stoch"]},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2
        assert any("Ошибка робота" in m for m in bot._notifier.messages)
        assert timeline.wait_boundaries == [False, False, True, True]
