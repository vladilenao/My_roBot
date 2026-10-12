"""Атомарный claim и причинность доставки: реальные SQLite, fake/local broker."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event, RLock, Thread

import pytest

from src.broker import create_addressable_journal_broker
from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.actions import CancelEntry, CloseTrade, EntryOrderType, OpenTrade
from src.trade_management.actions import action_from_payload
from src.trade_management.manager import DispatchDataEvidence, GAP_ENTRY_REASON, TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
READY = DispatchDataEvidence(True, True, True)
HOLD = DispatchDataEvidence(False, False, None)


def plan(uid="A"):
    return TradePlan(f"trade-{uid}", f"binding-{uid}", uid, "BUY", f"signal-{uid}",
        Decimal(100), Decimal(96), (TargetPlan("tp", Decimal(104), Decimal(1)),),
        ProfileSnapshot("levels_rr", "1", {}), NOW, "15m")


class AckBroker(BrokerPort):
    def __init__(self):
        self.actions = []
    def register_trade(self, value):
        pass
    def submit(self, action, now):
        self.actions.append(action)
        return ExecutionEvent(f"{action.command_id}:ack", action.command_id, action.command_id,
            action.trade_id, ExecutionStatus.ACK, 0, None, Decimal(0), now, "next-bar")


class Evidence:
    def __init__(self):
        self.lock = RLock()
        self.by_uid = {"A": HOLD, "B": READY}
    @contextmanager
    def guard(self):
        with self.lock:
            yield
    def inspect(self, uid, action, command):
        return self.by_uid.get(uid, HOLD)


def queue_entry(manager, uid):
    command = OpenTrade(f"open-{uid}", f"trade-{uid}", 0, "entry", 1)
    assert manager.submit_plan(plan(uid), command)
    return command


def status(storage, command):
    return storage.connection.execute("SELECT status FROM outbox WHERE command_id=?", (command,)).fetchone()[0]


def setup(storage, broker=None):
    broker = broker or AckBroker()
    manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: NOW)
    evidence = Evidence()
    manager.configure_dispatch_eligibility(evidence.inspect, evidence.guard)
    return manager, broker, evidence


def test_hold_a_does_not_block_eligible_b_or_consume_claim_limit(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, _ = setup(storage)
        queue_entry(manager, "A")
        queue_entry(manager, "B")
        manager.dispatch(NOW, limit=1)
        assert [action.command_id for action in broker.actions] == ["open-B"]
        assert status(storage, "open-A") == "PENDING"
        assert status(storage, "open-B") == "SENT"


def test_dependent_a_is_never_claimed(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, _ = setup(storage)
        queue_entry(manager, "A")
        assert storage.claim_outbox() == ()
        assert status(storage, "open-A") == "PENDING"
        assert broker.actions == []


def test_installed_guard_cannot_be_overridden_by_a_failed_reconfiguration(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, evidence = setup(storage)
        queue_entry(manager, "A")
        with pytest.raises(ValueError, match="уже установлен"):
            manager.configure_dispatch_eligibility(lambda *args: READY, evidence.guard)
        assert manager.dispatch(NOW) == ()
        assert broker.actions == []


@pytest.mark.parametrize("evidence", [("false", True, True), (True, 1, None), (True, True, "true")])
def test_unknown_or_coerced_evidence_is_not_a_default_true(evidence):
    with pytest.raises(ValueError):
        DispatchDataEvidence(*evidence)


def test_plain_dispatch_and_recovery_cannot_bypass_installed_eligibility(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        manager, _, _ = setup(storage)
        queue_entry(manager, "A")
        storage.connection.execute("UPDATE outbox SET status='CLAIMED'")
        storage.connection.commit()
    with Storage(path) as storage:
        manager, broker, _ = setup(storage)
        assert status(storage, "open-A") == "PENDING"
        assert manager.dispatch(NOW) == ()
        assert broker.actions == []
        assert status(storage, "open-A") == "PENDING"


def test_confirmed_guard_close_delivery_is_independent_of_candle_hold(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        broker = create_addressable_journal_broker(1000, [])
        clock = [NOW]
        manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: clock[0])
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        moment = NOW + timedelta(minutes=1)
        clock[0] = moment
        broker.track_bar(moment, {"A": (102, 102, 102, 102)}, {}, bar_times={"A": moment})
        for event in broker.drain_addressed_events():
            manager.consume(event)
        command = f"trade-A:{GAP_ENTRY_REASON}:open-A"
        assert status(storage, command) == "PENDING"
        evidence = Evidence()
        manager.configure_dispatch_eligibility(evidence.inspect, evidence.guard)
        clock[0] += timedelta(minutes=1)
        manager.dispatch(clock[0])
        assert status(storage, command) == "SENT"
        assert storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade-A'").fetchone()[0] == 1
        assert broker.trade_state("trade-A").confirmed_stop is not None
        # Доставка выхода — ACK будущего бара, не фиктивный fill при gap.
        assert storage.connection.execute("SELECT COUNT(*) FROM fills WHERE command_id=?", (command,)).fetchone()[0] == 0


def test_existing_guard_restart_restores_only_proven_close_at_prior_revision(tmp_path):
    path = tmp_path / "trades.sqlite3"
    moment = NOW + timedelta(minutes=1)
    command = f"trade-A:{GAP_ENTRY_REASON}:open-A"
    with Storage(path) as storage:
        broker = create_addressable_journal_broker(1000, [])
        manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: moment)
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        broker.track_bar(moment, {"A": (102, 102, 102, 102)}, {}, bar_times={"A": moment})
        for event in broker.drain_addressed_events():
            manager.consume(event)
        assert status(storage, command) == "PENDING"
    with Storage(path) as storage:
        broker = create_addressable_journal_broker(1000, [])
        manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: moment)
        assert manager.restore() == ()  # Не становится ordinary active recovery.
        trade = storage.load_trade("trade-A", include_terminal=True)
        assert trade.state.quantity == 1
        payload = json.loads(storage.connection.execute("SELECT payload_json FROM outbox WHERE command_id=?", (command,)).fetchone()[0])
        action = action_from_payload(payload)
        assert trade.state.state_revision == action.state_revision + 1
        # Узкое recovery-исключение восстанавливает broker state, но не меняет
        # command_id/payload или durable snapshot N+1.
        assert broker.trade_state("trade-A").revision == trade.state.state_revision
        evidence = Evidence()
        manager.configure_dispatch_eligibility(evidence.inspect, evidence.guard)
        result, = manager.dispatch(moment)
        assert result.status is ExecutionStatus.ACK
        assert broker.has_recovery_guard_close(action)
        next_bar = moment + timedelta(minutes=1)
        broker.track_bar(next_bar, {"A": (101, 101, 101, 101)}, {}, bar_times={"A": next_bar})
        close = next(event for event in broker.drain_addressed_events() if event.command_id == command)
        assert close.status is ExecutionStatus.FILL
        manager.consume(close)
        assert storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade-A'").fetchone()[0] == 0


def test_rejected_position_without_proven_guard_is_not_recovered(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, _, evidence = setup(storage)
        evidence.by_uid["A"] = READY
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        storage.connection.execute("UPDATE trades SET phase='REJECTED', state_revision=1")
        storage.connection.execute("UPDATE positions SET quantity=1,average_price='100'")
        storage.connection.commit()
        broker = create_addressable_journal_broker(1000, [])
        restarted = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: NOW)
        assert restarted.restore() == ()
        assert broker.trade_state("trade-A") is None


def test_protective_name_alone_is_not_an_independence_proof(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, evidence = setup(storage)
        evidence.by_uid["A"] = READY
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        storage.connection.execute("UPDATE trades SET phase='OPEN'")
        storage.connection.execute("UPDATE positions SET quantity=1,average_price='100'")
        storage.connection.commit()
        action = CloseTrade("named-protective", "trade-A", 0, "protective")
        manager.submit_action(action)
        evidence.by_uid["A"] = HOLD
        count = len(broker.actions)
        manager.dispatch(NOW)
        assert status(storage, action.command_id) == "PENDING"
        assert len(broker.actions) == count


@pytest.mark.parametrize("freshness", [False, None])
def test_recovered_uid_does_not_release_stale_or_unproven_intent(tmp_path, freshness):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, evidence = setup(storage)
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        evidence.by_uid["A"] = DispatchDataEvidence(True, True, freshness)
        assert manager.dispatch(NOW + timedelta(hours=3)) == ()
        assert status(storage, "open-A") == "PENDING"
        assert broker.actions == []


def test_changed_revision_after_recovery_does_not_send_old_command(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, evidence = setup(storage)
        queue_entry(manager, "A")
        storage.connection.execute("UPDATE trades SET state_revision=1")
        storage.connection.commit()
        evidence.by_uid["A"] = READY
        manager.dispatch(NOW)
        assert broker.actions == []
        assert status(storage, "open-A") == "PENDING"
        assert manager.dispatch_block_reasons["open-A"] == "stale-state-revision"


def test_repeated_dispatch_does_not_submit_or_reduce_twice(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, _ = setup(storage)
        queue_entry(manager, "B")
        manager.dispatch(NOW)
        assert manager.dispatch(NOW) == ()
        assert len(broker.actions) == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_cancel_waits_and_does_not_erase_entry_active_in_past_minutes(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        broker = create_addressable_journal_broker(1000, [])
        clock = [NOW]
        manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: clock[0])
        manager.submit_plan(plan(), OpenTrade("open-A", "trade-A", 0, "entry", 1,
                                             EntryOrderType.LIMIT, Decimal(100)))
        manager.dispatch(NOW)
        clock[0] = NOW + timedelta(hours=2)
        cancel = CancelEntry("cancel-A", "trade-A", 0, "entry-timeout")
        manager.submit_action(cancel)
        evidence = Evidence()
        manager.configure_dispatch_eligibility(evidence.inspect, evidence.guard)
        assert manager.dispatch(clock[0]) == ()
        assert status(storage, "cancel-A") == "PENDING"
        # Минуты до времени отмены восстанавливаются по прежней активной заявке.
        moment = NOW + timedelta(minutes=2)
        broker.track_bar(moment, {"A": (100, 99, 101, 100)}, {}, bar_times={"A": moment})
        events = broker.drain_addressed_events()
        assert any(event.status is ExecutionStatus.FILL and event.command_id == "open-A" for event in events)
        for event in events:
            manager.consume(event)
        evidence.by_uid["A"] = READY
        manager.dispatch(clock[0])
        assert status(storage, "cancel-A") == "PENDING"  # Теперь команда устарела.


def test_confirmed_broker_events_are_consumed_even_while_delivery_is_held(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, _ = setup(storage)
        queue_entry(manager, "A")
        event = ExecutionEvent("known-fill", "open-A", "open-A", "trade-A", ExecutionStatus.FILL,
            1, Decimal(100), Decimal(0), NOW, "confirmed-fill")
        assert manager.consume(event)
        assert not manager.consume(event)
        assert storage.connection.execute("SELECT quantity FROM positions").fetchone()[0] == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        manager.dispatch(NOW)
        assert broker.actions == []  # Только reconciliation известной доставки.
        assert status(storage, "open-A") == "SENT"


def test_ttl_cancel_can_run_with_work_hold_when_execution_prefix_is_complete(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager, broker, evidence = setup(storage)
        evidence.by_uid["A"] = READY
        queue_entry(manager, "A")
        manager.dispatch(NOW)
        manager.submit_action(CancelEntry("cancel-A", "trade-A", 0, "entry-timeout"))
        evidence.by_uid["A"] = DispatchDataEvidence(True, False, None)
        manager.dispatch(NOW + timedelta(hours=2))
        assert [action.command_id for action in broker.actions] == ["open-A", "cancel-A"]


def test_data_change_cannot_race_between_claim_check_and_local_submit(tmp_path):
    checking, attempted, changed = Event(), Event(), Event()
    evidence = Evidence()
    evidence.by_uid["B"] = READY
    class ObservedBroker(AckBroker):
        def submit(self, action, now):
            assert not changed.is_set()
            return super().submit(action, now)
    def inspect(uid, action, command):
        checking.set()
        assert attempted.wait(2)
        return evidence.by_uid[uid]
    def change():
        assert checking.wait(2)
        attempted.set()
        with evidence.lock:
            evidence.by_uid["B"] = HOLD
            changed.set()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        broker = ObservedBroker()
        manager = TradeManager(storage, broker, initial_balance=Decimal(1000), clock=lambda: NOW)
        queue_entry(manager, "B")
        manager.configure_dispatch_eligibility(inspect, evidence.guard)
        thread = Thread(target=change)
        thread.start()
        try:
            manager.dispatch(NOW)
            thread.join(2)
            assert changed.is_set()
            assert len(broker.actions) == 1
        finally:
            thread.join(2)
