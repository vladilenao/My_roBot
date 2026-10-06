"""Причинное OHLC-исполнение LIMIT, ожидание и восстановление accepted orders."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker.journal_broker import JournalBroker
from src.broker.port import ExecutionEvent, ExecutionStatus
from src.portfolio import ContractMeta, PositionManager
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import AddToTrade, CancelEntry, CloseTrade, EntryOrderType, MoveStop, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CURRENT_ALGORITHM, CostSnapshot, ProfileSnapshot, TargetPlan, TargetPriceBasis, TradePlan

D = Decimal
NOW = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
META = ContractMeta("instrument", 1, 100, 100, 100)


def _plan(side="BUY"):
    direction = 1 if side == "BUY" else -1
    return TradePlan(
        "trade", "assignment", "instrument", side, "signal", D(100), D(100 - direction * 4),
        (TargetPlan("tp-1", D(100 + direction * 4), D("0.5")),
         TargetPlan("tp-2", D(100 + direction * 8), D("0.5"))),
        ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="15m",
        algorithm_version=CURRENT_ALGORITHM, cost_snapshot=CostSnapshot(D(0), D(0)),
        entry_order_type=EntryOrderType.LIMIT,
    )


def _broker():
    broker = JournalBroker(None, PositionManager(initial_deposit=100000, max_risk_pct=2), [], clock=lambda: NOW)
    broker.set_contracts({"instrument": META})
    return broker


def _action(quantity=2):
    return OpenTrade("entry", "trade", 0, "entry", quantity, EntryOrderType.LIMIT, D(100))


def _bar(broker, values, minutes=1, **kwargs):
    broker.track_bar(NOW + timedelta(minutes=minutes), {"instrument": values}, {"instrument": META}, **kwargs)
    return broker.drain_addressed_events()


@pytest.mark.parametrize("profile,quantity,planned,remaining", [
    ("levels_rr", 1, (0, 1), 0), ("levels_rr", 3, (1, 2), 0),
    ("atr_trend", 1, (0, 0), 1), ("atr_trend", 3, (0, 0), 3),
    ("atr_trend", 8, (2, 2), 4),
])
def test_simulator_and_reducer_share_integer_allocations(tmp_path, profile, quantity, planned, remaining):
    broker = _broker()
    p = _plan()
    p = replace(p, profile=ProfileSnapshot(profile, "1", {}), targets=tuple(
        replace(t, share=D("0.25")) if profile == "atr_trend" else t for t in p.targets))
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=D(100000))
        manager.submit_plan(p, _action(quantity), price_step=D(1), step_cost=D(100))
        manager.dispatch(NOW)
        for event in _bar(broker, (100, 99, 100, 100)):
            manager.consume(event)
        assert tuple(row[0] for row in storage.connection.execute(
            "SELECT planned_quantity FROM targets ORDER BY target_index")) == planned
        for event in _bar(broker, (100, 99, 109, 108), minutes=2):
            manager.consume(event)
        recovered = storage.load_trade("trade", include_terminal=True)
        assert recovered.state.quantity == remaining
        assert tuple(row[0] for row in storage.connection.execute(
            "SELECT filled_quantity FROM targets ORDER BY target_index")) == planned


def test_pending_stop_acceptance_does_not_free_risk_and_resumes_until_effective_ack(tmp_path):
    broker = _broker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=D(100000))
        manager.submit_plan(_plan(), _action(), price_step=D(1), step_cost=D(100))
        manager.dispatch(NOW)
        for event in _bar(broker, (100, 99, 101, 100)):
            manager.consume(event)
        state = storage.load_trade("trade").state
        manager.submit_action(MoveStop("move", "trade", state.state_revision, "profile-stop", D(100)), market_close=D(105))
        accepted = manager.dispatch(NOW+timedelta(minutes=2))[0]
        assert accepted.reason == "next-bar"
        state = storage.load_trade("trade").state
        assert state.confirmed_stop == 96 and state.pending_stop == 100
        assert manager._budget_state(storage.connection).open_risk == 800
        restarted_broker = _broker()
        restarted = TradeManager(storage, restarted_broker, initial_balance=D(100000))
        restarted.restore()
        effects = _bar(restarted_broker, (102, 101, 103, 102), minutes=3)
        for event in effects:
            restarted.consume(event)
        assert len(effects) == 1 and effects[0].execution_id != accepted.execution_id
        assert storage.load_trade("trade").state.confirmed_stop == 100
        assert restarted._budget_state(storage.connection).open_risk == 0
        assert _bar(restarted_broker, (102, 101, 103, 102), minutes=3) == []


@pytest.mark.parametrize("observed", [False, True])
def test_same_intrabar_entry_bar_after_restart_cannot_invent_a_target(tmp_path, observed):
    broker = _broker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=D(100000))
        manager.submit_plan(_plan(), _action(), price_step=D(1), step_cost=D(100))
        manager.dispatch(NOW)
        for event in _bar(broker, (103, 99, 105, 101)):
            manager.consume(event)
        if observed:
            manager.observe_bars({"instrument": (103, 99, 105, 101)}, {"instrument": NOW+timedelta(minutes=1)})
        restarted_broker = _broker()
        restarted = TradeManager(storage, restarted_broker, initial_balance=D(100000))
        restarted.restore()
        assert _bar(restarted_broker, (103, 99, 105, 101)) == []
        assert storage.load_trade("trade").state.quantity == 2
        later = _bar(restarted_broker, (101, 100, 105, 104), minutes=2)
        assert len(later) == 1 and later[0].filled_quantity == 1


def test_market_exit_on_open_precedes_a_later_intrabar_stop():
    broker = _broker()
    broker.register_trade(replace(_plan(), targets=()))
    broker.submit(_action(), NOW)
    _bar(broker, (100, 99, 101, 100))
    state = broker.trade_state("trade")
    broker.submit(CloseTrade("close", "trade", state.revision, "signal-exit"), NOW+timedelta(minutes=2))
    events = _bar(broker, (101, 95, 150, 120), minutes=2)
    assert len(events) == 1 and events[0].command_id == "close" and events[0].price == 101


def test_open_fill_of_add_precedes_later_targets_and_rebases_their_allocations():
    broker = _broker()
    broker.register_trade(replace(_plan(), price_step=D(1)))
    broker.submit(_action(), NOW)
    _bar(broker, (100, 99, 101, 100))
    state = broker.trade_state("trade")
    broker.submit(AddToTrade("add", "trade", state.revision, "signal-add", 1, "limit", D(102)), NOW+timedelta(minutes=2))
    events = _bar(broker, (101, 99, 105, 104), minutes=2)
    assert [event.price for event in events] == [D(101), D(105)]
    assert [event.filled_quantity for event in events] == [1, 1]
    assert broker.manager.positions["trade"].qty == 2


@pytest.mark.parametrize("side,bar,expected", [
    ("BUY", (101, 100, 102, 101), D(100)),
    ("BUY", (99.7, 99.5, 101, 100), D("99.7")),
    ("SELL", (99, 98, 100, 99), D(100)),
    ("SELL", (100.3, 99, 100.5, 100), D("100.3")),
])
def test_touch_or_favourable_open_respects_limit(side, bar, expected):
    broker = _broker()
    broker.register_trade(_plan(side))
    assert broker.submit(_action(), NOW).status is ExecutionStatus.ACK
    fills = _bar(broker, bar)
    assert len(fills) == 1 and fills[0].status is ExecutionStatus.FILL
    assert fills[0].price == expected and fills[0].filled_quantity == 2
    assert (expected <= 100) if side == "BUY" else (expected >= 100)


def test_unreached_limit_keeps_waiting_and_fills_only_once():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    assert _bar(broker, (101, 100.2, 102, 101)) == []
    fills = _bar(broker, (101, 100, 102, 101), minutes=2)
    assert len(fills) == 1 and fills[0].price == 100
    assert _bar(broker, (101, 100, 102, 101), minutes=2) == []
    assert broker.manager.positions["trade"].qty == 2


def test_limit_cannot_create_initial_buy_past_absolute_goal():
    from dataclasses import replace

    plan = _plan()
    plan = replace(plan, profile=ProfileSnapshot("pattern_targets", "1", {}),
                   targets=tuple(replace(t, price_basis=TargetPriceBasis.ABSOLUTE) for t in plan.targets))
    broker = _broker()
    broker.register_trade(plan)
    broker.submit(_action(), NOW)
    assert _bar(broker, (109, 108.5, 110, 109.5)) == []
    entry = _bar(broker, (109, 99, 110, 101), minutes=2)[0]
    assert entry.price == 100
    assert [t.price for t in broker.trade_state("trade").plan.targets] == [104, 108]


def test_intrabar_entry_does_not_claim_an_earlier_high_as_target():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    events = _bar(broker, (103, 99, 109, 101))
    assert len(events) == 1 and events[0].command_id == "entry"
    assert broker.manager.positions["trade"].qty == 2
    later = _bar(broker, (101, 99, 104, 102), minutes=2)
    assert len(later) == 1 and later[0].reason == "tp:tp-1"


def test_at_open_entry_can_take_targets_without_cancelling_filled_order():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    events = _bar(broker, (100, 99, 109, 108))
    assert [e.reason for e in events] == ["entry", "tp:tp-1", "tp:tp-2"]
    assert all(e.status is ExecutionStatus.FILL for e in events)
    assert "trade" not in broker.manager.positions


def test_stop_wins_after_intrabar_fill_without_fictitious_target():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    events = _bar(broker, (103, 95, 109, 97))
    assert [e.reason for e in events] == ["entry", "protective"]
    assert events[-1].price == 96


def test_instrument_uses_its_own_bar_timestamp():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    # Другой инструмент продвинул общий tick, но эта свеча ещё до постановки.
    events = _bar(broker, (101, 99, 103, 100), bar_times={"instrument": NOW - timedelta(minutes=1)})
    assert events == []
    assert _bar(broker, (101, 99, 103, 100), minutes=2)[0].price == 100


def test_expired_limit_cancels_without_late_fill():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    events = _bar(broker, (99, 98, 101, 100), minutes=60)
    assert len(events) == 1 and events[0].status is ExecutionStatus.CANCEL
    assert events[0].reason == "entry-timeout"
    assert "trade" not in broker.manager.positions


def test_cancelled_limit_cannot_fill_later():
    broker = _broker()
    broker.register_trade(_plan())
    broker.submit(_action(), NOW)
    result = broker.submit(CancelEntry("cancel", "trade", 0, "entry-timeout"), NOW)
    assert result.status is ExecutionStatus.CANCEL
    broker.drain_addressed_events()
    assert _bar(broker, (100, 99, 102, 101)) == []


def _seed(manager):
    action = _action(3)
    reservation = ReservationCandidate("reservation", "trade", "entry", 0, "assignment", "instrument", "signal", D(1200), D(300))
    assert manager.submit_plan(_plan(), action, reservation=reservation, risk_budget=D(2000),
                               margin_budget=D(100000), price_step=D(1), step_cost=D(100))
    assert manager.dispatch(NOW)[0].status is ExecutionStatus.ACK


@pytest.mark.parametrize("partial", [False, True])
def test_ack_or_partial_limit_restores_remaining_quantity_without_resubmission(tmp_path, partial):
    path = tmp_path / "trades.sqlite3"
    with Storage(path, clock=lambda: NOW) as storage:
        manager = TradeManager(storage, _broker(), initial_balance=D(100000), clock=lambda: NOW)
        _seed(manager)
        if partial:
            manager.consume(ExecutionEvent("entry:partial-1", "entry", "entry", "trade",
                                           ExecutionStatus.PARTIAL, 1, D(100), D(0), NOW, "entry"))
    with Storage(path, clock=lambda: NOW) as storage:
        broker = _broker()
        manager = TradeManager(storage, broker, initial_balance=D(100000), clock=lambda: NOW)
        manager.restore()
        manager.restore()
        assert manager.dispatch(NOW) == ()
        events = _bar(broker, (100, 99, 102, 101))
        assert len(events) == 1
        assert events[0].filled_quantity == (2 if partial else 3)
        assert manager.consume(events[0])
        row = storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade'").fetchone()
        assert row[0] == 3
        assert storage.connection.execute("SELECT status FROM orders WHERE command_id='entry'").fetchone()[0] == "FILLED"
        assert not manager.consume(events[0])


def test_addition_has_its_own_limit_and_does_not_trigger_gap_guard_against_old_entry(tmp_path):
    with Storage(tmp_path / "trades.sqlite3", clock=lambda: NOW) as storage:
        broker = _broker()
        manager = TradeManager(storage, broker, initial_balance=D(100000), clock=lambda: NOW)
        _seed(manager)
        entry = _bar(broker, (100, 99, 101, 100))[0]
        manager.consume(entry)
        revision = storage.load_trade("trade").state.state_revision
        addition = AddToTrade("add", "trade", revision, "add", 1, EntryOrderType.LIMIT, D(102))
        assert manager.submit_action(addition)
        manager.dispatch(NOW + timedelta(minutes=1))
        events = _bar(broker, (103, 102, 103, 102), minutes=2)
        assert len(events) == 1 and events[0].price == 102
        manager.consume(events[0])
        assert storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade'").fetchone()[0] == 4
        assert storage.connection.execute("SELECT COUNT(*) FROM orders WHERE action_type='CLOSE'").fetchone()[0] == 0


def test_restart_does_not_extend_ack_deadline_or_leave_reservation(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path, clock=lambda: NOW) as storage:
        manager = TradeManager(storage, _broker(), initial_balance=D(100000), clock=lambda: NOW)
        _seed(manager)
    with Storage(path, clock=lambda: NOW + timedelta(minutes=60)) as storage:
        broker = _broker()
        manager = TradeManager(storage, broker, clock=lambda: NOW + timedelta(minutes=60))
        manager.restore()
        events = _bar(broker, (100, 99, 102, 101), minutes=60)
        assert len(events) == 1 and events[0].status is ExecutionStatus.CANCEL
        manager.consume(events[0])
        assert storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade'").fetchone()[0] == 0
        assert storage.connection.execute("SELECT status FROM reservations").fetchone()[0] == "RELEASED"
