from unittest.mock import MagicMock
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import logging

import pandas as pd
import pytest

from src.bot import TradingBot
from src.instruments import Instrument
from src.strategies.contracts import Assignment, Decision, SignalType
from src.trade_management.actions import AddToTrade, CancelEntry, CloseTrade, MoveStop, ReduceTrade


def _assign(*names: str, profile: str = "basic_levels", timeframe: str = "1h") -> list[Assignment]:
    return [
        Assignment(id=f"test-{profile}-{timeframe}-{index}-{name}", strategy=name, management="levels_rr", filter_profile=profile, timeframe=timeframe)
        for index, name in enumerate(names)
    ]


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
    """Фейк координатора сеток: тики 1..ticks, далее KeyboardInterrupt."""

    def __init__(self, ticks=1, fallback=1.0, timeframes=("1h",)):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframes = tuple(timeframes)
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        return {tf for tf in self.timeframes if bar_ready(tf)}

    def grid(self, timeframe):
        return self

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

    def refresh_if_new_candle(self, timeframe, force=False):
        self.refresh_calls += 1
        self.refresh_forces.append(force)
        if self.refresh_error:
            raise self.refresh_error

    def has_fresh_closed_bar(self, timeframe, now=None):
        return True

    def frame_for(self, instrument, timeframe):
        return self.frames.get(
            (instrument.ticker, timeframe),
            self.frames.get(instrument.ticker, pd.DataFrame()),
        )


class RecordingExecution:
    def __init__(self):
        self.decisions = []
        self.calls = []

    def execute(self, decision, instrument, *, filter_profile="", filtered_out=False, timeframe=""):
        self.decisions.append((decision, instrument))
        self.calls.append(
            {
                "filter_profile": filter_profile,
                "filtered_out": filtered_out,
                "timeframe": timeframe,
            }
        )


class PollingTimeline:
    def __init__(self, ticks=1, fallback=1.0, timeframes=("1h",)):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframes = tuple(timeframes)
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        ready = set()
        for tf in self.timeframes:
            while not bar_ready(tf):
                pass
            ready.add(tf)
        return ready

    def grid(self, timeframe):
        return self

    def bar_close(self, bar_start):
        return bar_start + timedelta(hours=1)

    def fallback_secs(self):
        return self.fallback


class TimeoutTimeline:
    def __init__(self, ticks=1, fallback=1.0, timeframes=("1h",)):
        self.ticks = ticks
        self.fallback = fallback
        self.timeframes = tuple(timeframes)
        self.wait_calls = 0
        self.wait_boundaries = []

    def wait_until_bar_published(
        self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True
    ):
        self.wait_calls += 1
        self.wait_boundaries.append(wait_boundary)
        if self.wait_calls > self.ticks:
            raise KeyboardInterrupt
        return {tf for tf in self.timeframes if bar_ready(tf)}

    def grid(self, timeframe):
        return self

    def bar_close(self, bar_start):
        return bar_start + timedelta(hours=1)

    def fallback_secs(self):
        return self.fallback


class LateBarCache(FakeCache):
    def __init__(self, publish_after, frames=None):
        super().__init__(frames=frames)
        self.publish_after = publish_after
        self.checks = 0

    def has_fresh_closed_bar(self, timeframe, now=None):
        self.checks += 1
        return self.checks >= self.publish_after


class NeverPublishCache(FakeCache):
    def has_fresh_closed_bar(self, timeframe, now=None):
        return False


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


def _make_bot(timeline, cache, execution, notifier, strategy, share=None, future=None, factory=None, heartbeat=None, context_cache=None, signal_filter=None, risk_manager=None, strategy_map=None, trade_manager=None, action_executor=None):
    return TradingBot(
        instruments=[],  # replace below
        notifier=notifier,
        strategy_map=strategy_map or {"macd_rsi_stoch": object(), "flat_triangle": object()},
        data_cache=cache,
        timeline=timeline,
        execution=execution,
        strategy_factory=factory or (lambda name, config: strategy),
        share_strategies=share or {},
        future_strategies=future or {},
        heartbeat_every_ticks=heartbeat,
        context_cache=context_cache,
        signal_filter=signal_filter,
        risk_manager=risk_manager,
        trade_manager=trade_manager,
        action_executor=action_executor,
    )


class TestTradingBot:
    def test_portfolio_admission_uses_stable_tick_candidate_order(self):
        trade_manager = MagicMock()
        admitted = []
        trade_manager.manage.return_value = ()
        trade_manager.actions_for_signal.side_effect = (
            lambda assignment, decision, instrument, *_args, **_kwargs:
            admitted.append((assignment.id, instrument.ticker, decision.event_id)) or ()
        )
        assignments = {
            "AAA": [Assignment("assignment-b", "macd_rsi_stoch", "levels_rr", priority=1, timeframe="1h")],
            "ZZZ": [Assignment("assignment-a", "macd_rsi_stoch", "levels_rr", priority=1, timeframe="1h")],
        }

        def admitted_for(instruments):
            bot = _make_bot(
                timeline=FakeTimeline(),
                cache=FakeCache(frames={"AAA": _df(), "ZZZ": _df()}),
                execution=RecordingExecution(),
                notifier=RecordingNotifier(),
                strategy=_make_strategy(),
                share=assignments,
                trade_manager=trade_manager,
            )
            bot._instruments = instruments
            bot.run()
            return admitted[:]

        forward = admitted_for([_inst("AAA", "AAA", "share"), _inst("ZZZ", "ZZZ", "share")])
        admitted.clear()
        reversed_order = admitted_for([_inst("ZZZ", "ZZZ", "share"), _inst("AAA", "AAA", "share")])

        assert forward == reversed_order
        assert [(assignment_id, instrument_id) for assignment_id, instrument_id, _ in forward] == [
            ("assignment-a", "ZZZ"),
            ("assignment-b", "AAA"),
        ]

    @pytest.mark.parametrize(
        "action",
        [
            CloseTrade("close", "trade-1", 0, "exit"),
            ReduceTrade("reduce", "trade-1", 0, "reduce", 1),
            MoveStop("move", "trade-1", 0, "move-stop", Decimal("99")),
            CancelEntry("cancel", "trade-1", 0, "cancel"),
        ],
    )
    def test_management_actions_bypass_entry_filter(self, action):
        signal_filter = MagicMock()
        action_executor = MagicMock()
        bot = _make_bot(
            timeline=FakeTimeline(), cache=FakeCache(), execution=RecordingExecution(),
            notifier=RecordingNotifier(), strategy=_make_strategy(), signal_filter=signal_filter,
        )
        bot._action_executor = action_executor

        bot._dispatch_management_actions(
            (action,), Decision(SignalType.BUY, 100.5), object(), _assign("macd_rsi_stoch")[0], _inst("SBER", "share"), "1h",
        )

        signal_filter.apply.assert_not_called()
        action_executor.submit.assert_called_once()

    def test_add_action_always_passes_entry_filter(self):
        signal_filter = MagicMock()
        signal_filter.apply.return_value = Decision(SignalType.BUY, 100.5)
        action_executor = MagicMock()
        bot = _make_bot(
            timeline=FakeTimeline(), cache=FakeCache(), execution=RecordingExecution(),
            notifier=RecordingNotifier(), strategy=_make_strategy(), signal_filter=signal_filter,
        )
        bot._action_executor = action_executor

        bot._dispatch_management_actions(
            (AddToTrade("add", "trade-1", 0, "add", 1),),
            Decision(SignalType.BUY, 100.5), object(), _assign("macd_rsi_stoch")[0], _inst("SBER", "share"), "1h",
        )

        signal_filter.apply.assert_called_once()
        action_executor.submit.assert_called_once()

    def test_decisions_notified_even_in_trading_mode(self):
        trade_manager = MagicMock()
        trade_manager.manage.return_value = ()
        trade_manager.actions_for_signal.return_value = ()
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=_make_strategy(decision=Decision(SignalType.HOLD, 100.5)),
            share={"SBER": _assign("macd_rsi_stoch")},
            trade_manager=trade_manager,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert trade_manager.actions_for_signal.call_count == 1
        assert len(execution.decisions) == 1
        assert execution.calls[0]["timeframe"] == "1h"
        assert execution.calls[0]["filter_profile"] == "basic_levels"

    def test_signal_management_exit_bypasses_entry_filter_in_cycle(self):
        signal_filter = MagicMock()
        action_executor = MagicMock()
        trade_manager = MagicMock()
        trade_manager.manage.return_value = ()
        trade_manager.actions_for_signal.return_value = (
            CloseTrade("close", "trade-1", 0, "raw-opposite-signal"),
        )
        bot = _make_bot(
            timeline=FakeTimeline(), cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(), notifier=RecordingNotifier(), strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")}, context_cache=MagicMock(),
            signal_filter=signal_filter, trade_manager=trade_manager,
            action_executor=action_executor,
        )
        bot._context_cache.get_context.return_value = object()
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        signal_filter.apply.assert_not_called()
        action_executor.submit.assert_called_once()

    def test_protection_runs_on_1m_before_a_hold_strategy(self):
        events = []
        trade_manager = MagicMock()
        trade_manager.manage.side_effect = lambda *args, **kwargs: events.append("manage") or ()
        post_tick = MagicMock(side_effect=lambda timeframes: events.append(f"protect:{next(iter(timeframes))}"))
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("1m", "1h")),
            cache=FakeCache(frames={
                ("SBER", "1m"): _df(),
                ("SBER", "1h"): _df(),
            }),
            execution=RecordingExecution(), notifier=RecordingNotifier(),
            strategy=_make_strategy(decision=Decision(SignalType.HOLD, 100.5)),
            share={"SBER": _assign("macd_rsi_stoch")}, trade_manager=trade_manager,
        )
        bot._post_tick = post_tick
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert events[0] == "protect:1m"
        assert trade_manager.manage.call_count == 2
        assert trade_manager.manage.call_args_list[0].kwargs["timeframe"] == "1m"

    def test_deleted_assignment_keeps_1m_management_subscription(self):
        trade_manager = MagicMock()
        trade_manager.manage.return_value = ()
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("1m",)),
            cache=FakeCache(frames={("SBER", "1m"): _df()}),
            execution=RecordingExecution(), notifier=RecordingNotifier(), strategy=_make_strategy(),
            share={}, trade_manager=trade_manager,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        trade_manager.manage.assert_called_once()
        assert trade_manager.manage.call_args.args[1] == []
        assert trade_manager.manage.call_args.kwargs["timeframe"] == "1m"

    def test_strategy_error_does_not_block_prior_1m_management(self):
        strategy = _make_strategy()
        strategy.compute.side_effect = RuntimeError("boom")
        trade_manager = MagicMock()
        trade_manager.manage.return_value = ()
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("1m", "1h")),
            cache=FakeCache(frames={
                ("SBER", "1m"): _df(),
                ("SBER", "1h"): _df(),
            }),
            execution=RecordingExecution(), notifier=RecordingNotifier(), strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")}, trade_manager=trade_manager,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert trade_manager.manage.call_args_list[0].kwargs["timeframe"] == "1m"

    def test_empty_1m_bar_does_not_run_management_or_strategy(self):
        trade_manager = MagicMock()
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("1m",)),
            cache=FakeCache(frames={("SBER", "1m"): pd.DataFrame()}),
            execution=RecordingExecution(), notifier=RecordingNotifier(), strategy=_make_strategy(),
            share={}, trade_manager=trade_manager,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        trade_manager.manage.assert_not_called()

    def test_1m_protection_does_not_analyze_a_1h_strategy(self):
        strategy = _make_strategy()
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("1m",)),
            cache=FakeCache(frames={("SBER", "1m"): _df()}),
            execution=RecordingExecution(), notifier=RecordingNotifier(), strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch", timeframe="1h")}, trade_manager=MagicMock(),
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        strategy.compute.assert_not_called()

    def test_fail_fast_on_unknown_strategy(self):
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("no_such_strategy")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        with pytest.raises(ValueError, match="no_such_strategy"):
            bot.run()

        assert bot._data_cache.refresh_calls == 0

    def test_fail_fast_when_bound_strategy_missing_from_strategy_map(self):
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"EDU6": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            future={"ED": _assign("harmonic_abcd")},
        )
        bot._instruments = [_inst("ED (Евро – Доллар) — ED-9.26", "EDU6", "future")]

        with pytest.raises(ValueError, match="harmonic_abcd"):
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
            future={"NG": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2

    def test_strategy_failure_does_not_block_next(self):
        failing = _make_strategy()
        failing.compute.side_effect = ValueError("boom")
        working = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(side_effect=[failing, working])
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"MUZ6": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=failing,
            future={"MU": _assign("macd_rsi_stoch", "flat_triangle")},
            factory=factory,
        )
        bot._instruments = [_inst("MU (base) — MUZ6", "MUZ6", "future")]

        bot.run()

        working.compute.assert_called_once()

    def test_bound_strategy_in_map_is_built(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(return_value=strategy)
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            strategy_map={
                "macd_rsi_stoch": object(),
                "flat_triangle": object(),
                "harmonic_abcd": object(),
            },
            factory=factory,
            share={"SBER": _assign("macd_rsi_stoch", "flat_triangle", "harmonic_abcd")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert any(call.args[0] == "harmonic_abcd" for call in factory.call_args_list)
        assert bot._strategy_cache[("harmonic_abcd", "1h")] is strategy
        assert len(execution.decisions) == 3

    def test_tick_error_notifies_trader(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}, refresh_error=RuntimeError("boom")),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert any("bot_debug.log" in m for m in bot._notifier.messages)

    def test_error_message_does_not_leak_technical_details(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}, refresh_error=RuntimeError("boom")),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        notified = [m for m in bot._notifier.messages if "Сбой" in m]
        assert len(notified) == 1
        assert "boom" not in notified[0]
        assert "обновление свечей таймфрейма 1h" in notified[0]
        assert "bot_debug.log" in notified[0]

    def test_rate_limit_error_is_not_notified(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}, refresh_error=RuntimeError("RESOURCE_EXHAUSTED 8")),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert not any("Сбой" in m for m in bot._notifier.messages)
        assert bot._errors_in_period == 1

    def test_heartbeat_every_n_ticks(self):
        bot = _make_bot(
            timeline=FakeTimeline(ticks=5),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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

            def refresh_if_new_candle(self, timeframe, force=False):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")

            def has_fresh_closed_bar(self, timeframe, now=None):
                return True

            def frame_for(self, instrument, timeframe):
                return _df()

        bot = _make_bot(
            timeline=FakeTimeline(ticks=2),
            cache=FailOnceCache(),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch")},
            heartbeat=1,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        heartbeats = [m for m in bot._notifier.messages if "Сердцебиение" in m]
        # ошибка не роняет тик: tick 1 выполнился со счётчиком ошибок, tick 2 — сброс
        assert len(heartbeats) == 2
        assert "ошибок за период — 1" in heartbeats[0]
        assert "ошибок за период — 0" in heartbeats[-1]

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
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert cache.checks >= 3
        assert len(execution.decisions) == 1

    def test_timeout_without_fresh_bar_skips_processing(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = NeverPublishCache(frames={"SBER": _df()})
        bot = _make_bot(
            timeline=TimeoutTimeline(ticks=1),
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 0
        assert strategy.compute.called is False

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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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
            share={"SBER": _assign("macd_rsi_stoch")},
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

            def refresh_if_new_candle(self, timeframe, force=False):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                self.ready = True

            def has_fresh_closed_bar(self, timeframe, now=None):
                return self.ready

            def frame_for(self, instrument, timeframe):
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
            share={"SBER": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert len(execution.decisions) == 2
        assert any("bot_debug.log" in m for m in bot._notifier.messages)
        assert timeline.wait_boundaries == [False, False, True, True]

    def test_empty_tick_skips_process_and_heartbeat(self):
        class FlagCache(FakeCache):
            def __init__(self, frames=None):
                super().__init__(frames=frames)
                self.flag = False

            def has_fresh_closed_bar(self, timeframe, now=None):
                return self.flag

        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        cache = FlagCache(frames={"SBER": _df()})
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=cache,
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")},
            heartbeat=1,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        cache.flag = False
        bot._tick(set())  # пустой тик: ни у одного ТФ свеча не закрылась
        assert len(execution.decisions) == 0
        assert not any("Сердцебиение" in m for m in bot._notifier.messages)

        cache.flag = True
        bot._tick({"1h"})
        assert len(execution.decisions) == 1

    def test_market_context_computed_once_per_instrument(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        context_cache = MagicMock()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch", "flat_triangle")},
            factory=lambda name, config: strategy,
            context_cache=context_cache,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert context_cache.get_context.call_count == 1

    def test_strategies_built_once_not_per_tick(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(return_value=strategy)
        bot = _make_bot(
            timeline=FakeTimeline(ticks=3),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")},
            factory=factory,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        # фабрика вызывается только при построении кэша (по разу на пару имя×ТФ), не на каждый тик
        assert factory.call_count == len(bot._strategy_cache)
        assert len(execution.decisions) == 3

    def test_fatal_runtime_error_in_strategy_reports_globally(self):
        failing = _make_strategy()
        failing.compute.side_effect = RuntimeError("boom")
        working = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(side_effect=[failing, working])
        notifier = RecordingNotifier()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"MUZ6": _df()}),
            execution=execution,
            notifier=notifier,
            strategy=failing,
            future={"MU": _assign("macd_rsi_stoch", "flat_triangle")},
            factory=factory,
        )
        bot._instruments = [_inst("MU (base) — MUZ6", "MUZ6", "future")]

        bot.run()

        # фатальная ошибка не скрывается локально — всплывает и обрабатывается как ошибка робота
        assert any("bot_debug.log" in m for m in notifier.messages)
        # в тексте уведомления названы инструмент, таймфрейм и стратегия
        assert any("анализ" in m and "macd_rsi_stoch" in m for m in notifier.messages)
        # _analyze прерван на первой стратегии — вторая в том же тике не выполнилась
        assert working.compute.called is False

    def test_legacy_risk_manager_is_not_applied_to_entry_events(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        risk = MagicMock()
        risk.apply.side_effect = RuntimeError("risk boom")
        notifier = RecordingNotifier()
        context_cache = MagicMock()
        context_cache.get_context.return_value = object()
        bot = _make_bot(
            timeline=FakeTimeline(ticks=1),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=notifier,
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")},
            context_cache=context_cache,
            risk_manager=risk,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        risk.apply.assert_not_called()
        assert not notifier.messages
        assert len(execution.decisions) == 1

    def test_duplicate_strategy_with_distinct_profiles_runs_both(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch", profile="raw") + _assign("macd_rsi_stoch")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        # обе привязки обрабатываются как независимые задачи с общим инстансом
        assert len(execution.decisions) == 2
        assert [c["filter_profile"] for c in execution.calls] == ["raw", "basic_levels"]
        assert strategy.decide.call_count == 2

    def test_unknown_filter_profile_fails_fast(self):
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=_make_strategy(),
            share={"SBER": _assign("macd_rsi_stoch", profile="no_such_profile")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        with pytest.raises(ValueError, match="no_such_profile"):
            bot.run()

        assert bot._data_cache.refresh_calls == 0

    def test_filtered_out_detected_and_passed_to_port(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        signal_filter = MagicMock()
        signal_filter.apply.side_effect = lambda decision, ctx, profile_name="basic_levels", instrument="", timeframe="": replace(
            decision, signal_type=SignalType.HOLD
        )
        context_cache = MagicMock()
        context_cache.get_context.return_value = object()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch")},
            context_cache=context_cache,
            signal_filter=signal_filter,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        # фильтр получил профиль привязки и объект инструмента с ТФ, порт — профиль и признак отклонения
        assert signal_filter.apply.call_args.kwargs["profile_name"] == "basic_levels"
        assert signal_filter.apply.call_args.kwargs["instrument"] is bot._instruments[0]
        assert signal_filter.apply.call_args.kwargs["timeframe"] == "1h"
        assert execution.calls[0]["filtered_out"] is True
        assert execution.calls[0]["filter_profile"] == "basic_levels"

    def test_passing_signal_is_not_marked_filtered_out(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()
        signal_filter = MagicMock()
        signal_filter.apply.side_effect = lambda decision, ctx, profile_name="basic_levels", instrument="", timeframe="": decision
        context_cache = MagicMock()
        context_cache.get_context.return_value = object()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"SBER": _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch", profile="raw")},
            context_cache=context_cache,
            signal_filter=signal_filter,
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        assert signal_filter.apply.call_args.kwargs["profile_name"] == "raw"
        assert execution.calls[0]["filtered_out"] is False
        assert execution.calls[0]["filter_profile"] == "raw"

    def test_only_crossed_timeframe_is_processed(self):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        execution = RecordingExecution()

        class Ready15mTimeline(FakeTimeline):
            def wait_until_bar_published(self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True):
                self.wait_calls += 1
                self.wait_boundaries.append(wait_boundary)
                if self.wait_calls > self.ticks:
                    raise KeyboardInterrupt
                return {"15m"}  # на тике закрылась только 15m-свеча

        bot = _make_bot(
            timeline=Ready15mTimeline(timeframes=("15m", "1h")),
            cache=FakeCache(frames={("SBER", "15m"): _df(), ("SBER", "1h"): _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=strategy,
            share={"SBER": _assign("macd_rsi_stoch", timeframe="15m") + _assign("flat_triangle", timeframe="1h")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        # обработана только 15m-привязка, с её ТФ в decide и в порте
        assert len(execution.decisions) == 1
        assert strategy.decide.call_args.kwargs["timeframe"] == "15m"
        assert execution.calls[0]["timeframe"] == "15m"

    def test_duplicate_strategy_on_different_timeframes_gets_separate_instances(self):
        inst_15m = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        inst_1h = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        execution = RecordingExecution()
        factory = MagicMock(side_effect=[inst_15m, inst_1h])
        bot = _make_bot(
            timeline=FakeTimeline(timeframes=("15m", "1h")),
            cache=FakeCache(frames={("SBER", "15m"): _df(), ("SBER", "1h"): _df()}),
            execution=execution,
            notifier=RecordingNotifier(),
            strategy=inst_15m,
            factory=factory,
            share={"SBER": _assign("macd_rsi_stoch", timeframe="15m") + _assign("macd_rsi_stoch", timeframe="1h")},
        )
        bot._instruments = [_inst("SBER", "SBER", "share")]

        bot.run()

        # два инстанса по ключу (имя, ТФ): состояние ТФ не смешивается
        assert set(bot._strategy_cache) == {("macd_rsi_stoch", "15m"), ("macd_rsi_stoch", "1h")}
        assert bot._strategy_cache[("macd_rsi_stoch", "15m")] is inst_15m
        assert bot._strategy_cache[("macd_rsi_stoch", "1h")] is inst_1h
        assert inst_15m.decide.call_args.kwargs["timeframe"] == "15m"
        assert inst_1h.decide.call_args.kwargs["timeframe"] == "1h"
        assert len(execution.decisions) == 2


def _ta_frame():
    return pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=3, freq="1h"),
        "open": [100.0] * 3,
        "high": [101.0] * 3,
        "low": [99.0] * 3,
        "close": [100.5] * 3,
        "volume": [1000] * 3,
        "rsi14": [54.5, 54.5, 54.5],
        "stochk14": [37.1, 37.1, 37.1],
        "macdh": [-0.00236, -0.00236, -0.00236],
        "macd_rsi_stoch_signal": [0, 0, 0],
    })


class TestAnalysisLogging:
    """Проверка диагностических логов анализа: данные, стратегии, сводка, tick-id."""

    @staticmethod
    def _future_bot(strategy=None, *, share=None, future=None):
        strategy = strategy or _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"NGV6": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=strategy,
            share=share,
            future=future or {"NG": _assign("macd_rsi_stoch")},
        )
        bot._instruments = [
            Instrument(
                label="NG (Природный газ) — NG-10.26",
                ticker="NGV6",
                instrument_type="future",
                short_name="NG-10.26",
            )
        ]
        return bot

    def test_debug_bar_line_and_summary(self, caplog):
        caplog.set_level(logging.DEBUG, logger="src.bot")
        self._future_bot().run()

        text = caplog.text
        assert "NG-10.26 1h | получено свечей=10" in text
        assert "итог: macd_rsi_stoch=HOLD" in text

    def test_debug_decision_line_includes_indicator_digest(self, caplog):
        strategy = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        strategy.compute.return_value = _ta_frame()
        caplog.set_level(logging.DEBUG, logger="src.bot")
        self._future_bot(strategy=strategy).run()

        assert "macd_rsi_stoch → HOLD" in caplog.text
        assert "rsi14=54.500" in caplog.text
        assert "macdh=-0.002" in caplog.text

    def test_all_lines_of_tick_share_same_tick_id(self, caplog):
        caplog.set_level(logging.DEBUG, logger="src.bot")
        self._future_bot().run()

        ids = {
            rec.correlation_id
            for rec in caplog.records
            if rec.message.startswith("NG-10.26 1h")
        }
        assert ids and len(ids) == 1
        import re
        assert re.fullmatch(r"[0-9a-f]{8}", next(iter(ids)))
        assert all(rec.correlation_id == next(iter(ids)) for rec in caplog.records if rec.message.startswith("NG-10.26 1h"))

    def test_filtered_out_is_logged(self, caplog):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.5))
        signal_filter = MagicMock()
        signal_filter.apply.side_effect = lambda decision, ctx, profile_name="basic_levels", instrument="", timeframe="": replace(
            decision, signal_type=SignalType.HOLD
        )
        context_cache = MagicMock()
        context_cache.get_context.return_value = object()
        bot = _make_bot(
            timeline=FakeTimeline(),
            cache=FakeCache(frames={"NGV6": _df()}),
            execution=RecordingExecution(),
            notifier=RecordingNotifier(),
            strategy=strategy,
            future={"NG": _assign("macd_rsi_stoch")},
            context_cache=context_cache,
            signal_filter=signal_filter,
        )
        bot._instruments = [
            Instrument(
                label="NG (Природный газ) — NG-10.26",
                ticker="NGV6",
                instrument_type="future",
                short_name="NG-10.26",
            )
        ]
        caplog.set_level(logging.DEBUG, logger="src.bot")
        bot.run()

        assert "отклонён фильтром" in caplog.text
        assert "итог: macd_rsi_stoch=HOLD" in caplog.text
