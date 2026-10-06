"""Комиссия факта, provenance, gap и поздняя идемпотентная корректировка."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker.journal_broker import JournalBroker
from src.broker.port import ExecutionEvent, ExecutionStatus, FeeAdjustment, FeeSource
from src.portfolio import ContractMeta, PositionManager
from src.trade_journal.storage import Storage
from src.trade_management.actions import EntryOrderType, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CURRENT_ALGORITHM, CostSnapshot, ProfileSnapshot, TargetPlan, TradePlan

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _plan(side="BUY"):
    d = 1 if side == "BUY" else -1
    return TradePlan("trade", "assignment", "instrument", side, "signal", D(100), D(100 - 4*d),
                     (TargetPlan("tp-1", D(100+4*d), D("0.5")), TargetPlan("tp-2", D(100+8*d), D("0.5"))),
                     ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="15m",
                     algorithm_version=CURRENT_ALGORITHM, cost_snapshot=CostSnapshot(D("1.5"), D(1)),
                     entry_order_type=EntryOrderType.LIMIT)


def _manager(storage, *, side="BUY", quantity=2):
    broker = JournalBroker(None, PositionManager(initial_deposit=250000, max_risk_pct=2), [], clock=lambda: NOW)
    broker.set_contracts({"instrument": ContractMeta("instrument", 1, 100, 100, 100)})
    manager = TradeManager(storage, broker, initial_balance=D(250000), clock=lambda: NOW)
    action = OpenTrade("entry", "trade", 0, "entry", quantity, EntryOrderType.LIMIT, D(100))
    assert manager.submit_plan(_plan(side), action, price_step=D(1), step_cost=D(100))
    manager.dispatch(NOW)
    return manager, broker


@pytest.mark.parametrize("fee,expected,source", [(D(0), D(0), "broker"), (None, D(3), "configured")])
def test_known_zero_and_missing_fee_are_distinct(tmp_path, fee, expected, source):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _ = _manager(storage)
        event = ExecutionEvent("fill", "entry", "entry", "trade", ExecutionStatus.FILL, 2, D(100), fee, NOW, "entry")
        assert manager.consume(event)
        assert not manager.consume(event)
        row = storage.connection.execute("SELECT fee,fee_source FROM fills").fetchone()
        assert D(row[0]) == expected and row[1] == source
        state = storage.load_trade("trade").state
        assert state.fees == expected and state.fees_known
        assert state.initial_stop_distance == 4 and state.max_quantity == 2
        assert D(storage.connection.execute("SELECT balance FROM account").fetchone()[0]) == 250000 - expected


def test_entry_and_two_targets_charge_thirty_not_forty(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker = _manager(storage, quantity=10)
        broker.track_bar(NOW+timedelta(minutes=1), {"instrument": (100, 99, 104, 103)}, {})
        first = broker.drain_addressed_events()
        assert [e.fee for e in first] == [D(15), D("7.5")]
        for event in first:
            manager.consume(event)
        broker.track_bar(NOW+timedelta(minutes=2), {"instrument": (108, 107, 109, 108)}, {})
        last = broker.drain_addressed_events()
        assert len(last) == 1 and last[0].fee == D("7.5")
        manager.consume(last[0])
        gross, fees, net = storage.connection.execute("SELECT realized_pnl,fees,net_realized_pnl FROM positions").fetchone()
        assert (D(gross), D(fees), D(net)) == (D(6000), D(30), D(5970))
        assert set(r[0] for r in storage.connection.execute("SELECT fee_source FROM fills")) == {"configured"}


def test_adverse_stop_gap_is_diagnostic_not_a_second_charge(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _ = _manager(storage, side="SELL")
        manager.consume(ExecutionEvent("entry-fill", "entry", "entry", "trade", ExecutionStatus.FILL,
                                       2, D(100), D(3), NOW, "entry"))
        cmd = "trade:pv:stop:2026-01-01T00:01:00"
        event = ExecutionEvent("stop-fill", cmd, cmd, "trade", ExecutionStatus.FILL,
                               2, D(105), D(3), NOW+timedelta(minutes=1), "protective",
                               reference_price=D(104), reference_kind="stop", order_side="BUY", slippage_source="simulated")
        assert manager.consume(event)
        assert storage.connection.execute("SELECT slippage_amount FROM fills WHERE execution_id='stop-fill'").fetchone()[0] == "200"
        gross, fees, net = storage.connection.execute("SELECT realized_pnl,fees,net_realized_pnl FROM positions").fetchone()
        assert (D(gross), D(fees), D(net)) == (D(-1000), D(6), D(-1006))


def test_late_fee_adjustment_refunds_difference_and_repeats_once(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        manager, _ = _manager(storage)
        manager.consume(ExecutionEvent("fill", "entry", "entry", "trade", ExecutionStatus.FILL,
                                       2, D(100), None, NOW, "entry"))
        adjustment = FeeAdjustment("broker-update-1", "fill", D(2), NOW+timedelta(minutes=1))
        assert manager.consume(adjustment)
        assert not manager.consume(adjustment)
        row = storage.connection.execute("SELECT quantity,average_price,realized_pnl,fees,net_realized_pnl FROM positions").fetchone()
        assert row[0] == 2 and tuple(D(x) for x in row[1:]) == (D(100), D(0), D(2), D(-2))
        assert D(storage.connection.execute("SELECT balance FROM account").fetchone()[0]) == 249998
        assert storage.connection.execute("SELECT fee_source FROM fills").fetchone()[0] == "broker"
        with pytest.raises(ValueError, match="conflicting"):
            manager.consume(FeeAdjustment("broker-update-1", "fill", D(5), NOW))
        assert D(storage.connection.execute("SELECT fees FROM account").fetchone()[0]) == 2
    with Storage(path) as storage:
        manager = TradeManager(storage, object())
        assert not manager.consume(adjustment)


def test_unknown_legacy_fee_is_preserved_as_unknown(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _ = _manager(storage)
        event = ExecutionEvent("fill", "entry", "entry", "trade", ExecutionStatus.FILL,
                               2, D(100), D(0), NOW, "entry", fee_source=FeeSource.UNKNOWN)
        manager.consume(event)
        assert storage.connection.execute("SELECT fee,fee_source FROM fills").fetchone() == ("0", "unknown")
        assert not storage.load_trade("trade").state.fees_known


def test_favourable_price_deviation_is_negative_and_not_added_to_pnl(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _ = _manager(storage)
        event = ExecutionEvent("fill", "entry", "entry", "trade", ExecutionStatus.FILL, 2, D(99), D(0), NOW, "entry",
                               reference_price=D(100), reference_kind="signal", order_side="BUY", slippage_source="simulated")
        manager.consume(event)
        assert storage.connection.execute("SELECT slippage_amount FROM fills").fetchone()[0] == "-200"
        assert storage.connection.execute("SELECT realized_pnl,fees FROM account").fetchone() == ("0", "0")


def test_failure_after_financial_write_rolls_back_every_dependent_record(tmp_path, monkeypatch):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _ = _manager(storage)
        event = ExecutionEvent("fill", "entry", "entry", "trade", ExecutionStatus.FILL, 2, D(100), None, NOW, "entry")
        original = manager._reducer._update_target
        def broken(*args):
            raise RuntimeError("injected failure")
        monkeypatch.setattr(manager._reducer, "_update_target", broken)
        with pytest.raises(RuntimeError, match="injected"):
            manager.consume(event)
        assert storage.connection.execute("SELECT quantity,fees FROM positions").fetchone() == (0, "0")
        assert storage.connection.execute("SELECT balance FROM account").fetchone()[0] == "250000"
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        assert storage.connection.execute("SELECT COUNT(*) FROM trade_measurements").fetchone()[0] == 0
        monkeypatch.setattr(manager._reducer, "_update_target", original)
        assert manager.consume(event)
        assert D(storage.connection.execute("SELECT fees FROM account").fetchone()[0]) == 3
        assert not manager.consume(event)
