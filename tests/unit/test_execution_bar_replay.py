"""Исполнение доступных минут по порядку через настоящий runtime и SQLite."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import run
from src.portfolio import ContractMeta
from src.trade_management.actions import MoveStop, OpenTrade
from src.trade_management.models import CostSnapshot, PlanEconomics, ProfileSnapshot, TargetPlan, TradePlan


D = Decimal
NOW = datetime(2026, 10, 6, 15, 14, tzinfo=timezone.utc)
META = ContractMeta("BRX6", 0.01, 8.49309, 0, 0)
INSTRUMENT = ("BR", "BRX6", "future", "BR-11.26")


def frame(*bars):
    return pd.DataFrame([
        (NOW + timedelta(minutes=minute), *prices) for minute, prices in bars
    ], columns=["datetime", "open", "low", "high", "close"])


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "trading_enabled", lambda: True)
    monkeypatch.setattr(run, "_load_contracts_metadata", lambda *args, **kwargs: {
        "BRX6": META, "GAZP": ContractMeta("GAZP", 1, 1, 0, 0),
    })
    monkeypatch.setattr(run, "print_contract_metadata", lambda *args: None)
    cache = SimpleNamespace(frame=frame((-1, (98.35, 98.3, 98.4, 98.35))))
    cache.frames = {}
    cache.frame_for = lambda instrument, timeframe: cache.frames.get(instrument.ticker, cache.frame)
    runtimes = []

    def build(instruments=(INSTRUMENT,)):
        runtime = run._build_runtime(instruments, cache, MagicMock(), tmp_path, clock=lambda: NOW)
        runtimes.append(runtime)
        return runtime

    yield build, cache
    for runtime in runtimes:
        if runtime.storage.connection:
            runtime.storage.close()


def submit(runtime, *, targets=()):
    plan = TradePlan(
        "trade", "assignment", "BRX6", "SELL", "signal", D("98.35"), D("98.75"), targets,
        ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="5m",
        algorithm_version="economics-v2", entry_order_type="limit", price_step=D("0.01"),
        cost_snapshot=CostSnapshot(D("1.5"), D(1)),
        economics=PlanEconomics(5, D("1698.618"), None, D("1698.618"), None),
    )
    runtime.trade_manager.submit_plan(
        plan, OpenTrade("entry", "trade", 0, "entry", 5, "limit", D("98.35")),
        price_step=D("0.01"), step_cost=D("8.49309"),
    )
    runtime.trade_manager.dispatch(NOW)


def open_trade(build, cache, *, targets=()):
    runtime = build()
    runtime.post_tick({"1m"})  # Only the latest warm-up minute is observed.
    submit(runtime, targets=targets)
    cache.frame = frame((0, (98.35, 98.3, 98.4, 98.35)))
    runtime.post_tick({"1m"})
    assert runtime.storage.load_trade("trade").state.quantity == 5
    return runtime


def exit_fill(runtime):
    return runtime.storage.connection.execute(
        "SELECT f.price,f.executed_at,o.action_type FROM fills f JOIN orders o USING(order_id) "
        "WHERE o.action_type!='OPEN' ORDER BY f.rowid LIMIT 1"
    ).fetchone()


def test_delayed_batch_stops_on_first_crossing_and_repeats_do_not_change_pnl(runtime_factory):
    build, cache = runtime_factory
    runtime = open_trade(build, cache)
    # Unsorted backfill: the intermediate minute reaches the stop before the
    # last minute opens much higher. The old runtime closed at 99.81 instead.
    cache.frame = frame(
        (3, (99.81, 99.7, 99.9, 99.8)),
        (0, (98.35, 98.3, 98.4, 98.35)),
        (2, (98.65, 98.41, 99.37, 99.31)),
        (1, (98.35, 98.3, 98.6, 98.5)),
    )
    runtime.post_tick({"1m"})

    price, stamp, kind = exit_fill(runtime)
    assert D(price) == D("98.75") and kind == "STOP"
    assert datetime.fromisoformat(stamp) == NOW + timedelta(minutes=2)
    assert D(runtime.storage.connection.execute("SELECT net_realized_pnl FROM positions").fetchone()[0]) == D("-1713.618")
    before = runtime.storage.connection.execute("SELECT * FROM fills").fetchall()
    runtime.post_tick({"1m"})
    assert runtime.storage.connection.execute("SELECT * FROM fills").fetchall() == before


def test_real_missing_minutes_keep_gap_execution_and_partial_observations(runtime_factory):
    build, cache = runtime_factory
    runtime = open_trade(build, cache)
    cache.frame = frame((3, (99.81, 99.7, 99.9, 99.8)))

    runtime.post_tick({"1m"})

    assert D(exit_fill(runtime)[0]) == D("99.81")
    assert D(runtime.storage.connection.execute("SELECT net_realized_pnl FROM positions").fetchone()[0]) == D("-6214.95570")
    assert runtime.storage.connection.execute("SELECT coverage FROM trade_measurements").fetchone()[0] == "partial"


def test_target_in_earlier_minute_wins_over_later_stop(runtime_factory):
    build, cache = runtime_factory
    runtime = open_trade(build, cache, targets=(TargetPlan("tp", D("97.95"), D(1), D("0.40")),))
    cache.frame = frame(
        (1, (98.3, 97.9, 98.4, 98.0)),
        (2, (99.81, 99.7, 99.9, 99.8)),
    )

    runtime.post_tick({"1m"})

    price, stamp, kind = exit_fill(runtime)
    assert D(price) == D("97.95") and kind.startswith("TARGET")
    assert datetime.fromisoformat(stamp) == NOW + timedelta(minutes=1)
    assert runtime.storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_instruments_with_different_available_minutes_share_chronological_order(runtime_factory, monkeypatch):
    build, cache = runtime_factory
    runtime = build((INSTRUMENT, ("Газпром", "GAZP", "share")))
    runtime.post_tick({"1m"})
    broker = runtime.trade_manager._broker
    track = MagicMock(wraps=broker.track_bar)
    monkeypatch.setattr(broker, "track_bar", track)
    mark = MagicMock(wraps=runtime.trade_manager.mark_to_market)
    monkeypatch.setattr(runtime.trade_manager, "mark_to_market", mark)
    cache.frames = {
        "BRX6": frame((2, (98.5, 98.4, 98.6, 98.5)), (0, (98.35, 98.3, 98.4, 98.35))),
        "GAZP": frame((3, (100, 99, 101, 100)), (1, (99, 98, 100, 99))),
    }

    runtime.post_tick({"1m"})

    assert [call.args[0] for call in track.call_args_list] == [NOW + timedelta(minutes=i) for i in range(4)]
    assert [set(call.args[1]) for call in track.call_args_list] == [{"BRX6"}, {"GAZP"}, {"BRX6"}, {"GAZP"}]
    assert mark.call_args.args[0] == {"BRX6": 98.5, "GAZP": 100.0}
    runtime.post_tick({"1m"})
    assert track.call_count == 4


def test_restart_replays_only_after_durable_observation(runtime_factory):
    build, cache = runtime_factory
    previous = open_trade(build, cache)
    runtime = build()
    # A corrected/old warm-up candle must not hit the restored stop again.
    cache.frame = frame(
        (0, (99.81, 99.7, 99.9, 99.8)),
        (1, (98.35, 98.3, 98.6, 98.5)),
        (2, (98.65, 98.41, 99.37, 99.31)),
        (3, (99.81, 99.7, 99.9, 99.8)),
    )

    runtime.post_tick({"1m"})

    assert D(exit_fill(runtime)[0]) == D("98.75")
    assert runtime.storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert previous.storage.connection.execute("SELECT quantity FROM positions").fetchone()[0] == 0


def test_restored_pending_entry_replays_from_submission_not_warmup(runtime_factory):
    build, cache = runtime_factory
    previous = build()
    submit(previous)
    runtime = build()
    cache.frame = frame(
        (-2, (99.81, 99.7, 99.9, 99.8)),
        (0, (98.35, 98.3, 98.4, 98.35)),
        (1, (98.65, 98.41, 99.37, 99.31)),
        (2, (99.81, 99.7, 99.9, 99.8)),
    )

    runtime.post_tick({"1m"})

    assert D(exit_fill(runtime)[0]) == D("98.75")
    entry = runtime.storage.connection.execute("SELECT executed_at FROM fills ORDER BY rowid LIMIT 1").fetchone()[0]
    assert datetime.fromisoformat(entry) == NOW


def test_restart_finishes_execution_minute_without_replaying_older_bars(runtime_factory):
    build, cache = runtime_factory
    previous = build()
    submit(previous)
    broker = previous.trade_manager._broker
    broker.track_bar(NOW, {"BRX6": (98.35, 98.3, 98.4, 98.35)}, {"BRX6": META})
    for event in broker.drain_addressed_events():
        previous.trade_manager.consume(event)
    # Simulate a stop between the durable fill and its holding observation.
    runtime = build()
    cache.frame = frame(
        (-1, (99.81, 99.7, 99.9, 99.8)),
        (0, (98.35, 98.3, 98.4, 98.35)),
        (1, (98.65, 98.41, 99.37, 99.31)),
    )

    runtime.post_tick({"1m"})

    assert D(exit_fill(runtime)[0]) == D("98.75")
    assert datetime.fromisoformat(exit_fill(runtime)[1]) == NOW + timedelta(minutes=1)
    observations = runtime.storage.connection.execute(
        "SELECT bar_id FROM trade_market_observations WHERE timeframe='1m' ORDER BY bar_id"
    ).fetchall()
    assert [datetime.fromisoformat(row[0]) for row in observations] == [NOW, NOW + timedelta(minutes=1)]


def test_pending_stop_change_does_not_apply_to_earlier_backfill(runtime_factory):
    build, cache = runtime_factory
    runtime = open_trade(build, cache)
    state = runtime.storage.load_trade("trade").state
    runtime.trade_manager.submit_action(MoveStop("move", "trade", state.state_revision, "tighten", D("98.50")), market_close=D("98.35"))
    runtime.trade_manager.dispatch(NOW + timedelta(minutes=2))
    runtime = build()
    cache.frame = frame(
        (1, (98.35, 98.3, 98.6, 98.4)),
        (2, (98.35, 98.3, 98.6, 98.4)),
    )

    runtime.post_tick({"1m"})

    price, stamp, _ = exit_fill(runtime)
    assert D(price) == D("98.50")
    assert datetime.fromisoformat(stamp) == NOW + timedelta(minutes=2)


def test_failed_execution_write_is_retried_before_next_minute(runtime_factory, monkeypatch):
    build, cache = runtime_factory
    runtime = open_trade(build, cache)
    cache.frame = frame(
        (1, (98.65, 98.41, 99.37, 99.31)),
        (2, (99.81, 99.7, 99.9, 99.8)),
    )
    consume = runtime.trade_manager.consume
    fail_once = True

    def failing_consume(event):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("temporary database failure")
        return consume(event)

    monkeypatch.setattr(runtime.trade_manager, "consume", failing_consume)
    runtime.post_tick({"1m"})
    assert exit_fill(runtime) is None

    runtime.post_tick({"1m"})

    assert D(exit_fill(runtime)[0]) == D("98.75")
    assert datetime.fromisoformat(exit_fill(runtime)[1]) == NOW + timedelta(minutes=1)
    assert runtime.storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
