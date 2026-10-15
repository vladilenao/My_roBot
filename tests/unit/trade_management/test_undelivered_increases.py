"""Снятие недоставленных входов и доборов (cancel-stale-undelivered-entries).

Настоящие ``Storage``, reducer и ``TradeManager``; брокер — подставной. Команда,
удержанная в шаге своего создания, со следующего шага неактуальна навсегда.
"""

import csv
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.portfolio.models import ContractMeta
from src.trade_journal.export import REASON_LABELS, CsvExporter
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import AddToTrade, CloseTrade, OpenTrade
from src.trade_management.manager import DispatchDataEvidence, TradeManager, created_before_step
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

STEP = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
NEXT = STEP + timedelta(seconds=1)
READY = DispatchDataEvidence(True, True, True)
HOLD = DispatchDataEvidence(False, False, None)


class FillBroker(BrokerPort):
    def __init__(self):
        self.actions = []

    def register_trade(self, plan):
        pass

    def contract_for(self, ticker):
        return ContractMeta(ticker=ticker, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0,
                            expiration_date=None)

    def submit(self, action, now):
        self.actions.append(action)
        return ExecutionEvent(f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
                              ExecutionStatus.FILL, action.quantity, Decimal(100), Decimal(0), now, action.reason)


class Evidence:
    def __init__(self, value):
        self.value, self.lock = value, RLock()

    @contextmanager
    def guard(self):
        with self.lock:
            yield

    def inspect(self, uid, action, command):
        return self.value


def plan(trade="trade-1", assignment="binding-1"):
    return TradePlan(trade, assignment, "PHOR", "BUY", f"signal-{trade}", Decimal(100), Decimal(96),
                     (TargetPlan("tp", Decimal(104), Decimal(1)),), ProfileSnapshot("levels_rr", "1", {}), STEP, "1h")


def reserve(storage, trade, order, risk):
    storage.reserve_candidates((ReservationCandidate(
        reservation_id=f"reservation-{order}", trade_id=trade, order_id=order, priority=0, assignment_id="binding-1",
        instrument_id="PHOR", signal_id=f"signal-{trade}", risk_amount=Decimal(risk), margin_amount=Decimal(0)),),
        risk_budget=Decimal(1900), margin_budget=Decimal(1900))


def manager(storage, evidence, observed=None, broker=None):
    broker = broker or FillBroker()
    result = TradeManager(storage, broker, initial_balance=Decimal(100000), clock=lambda: STEP,
                          execution_observer=(lambda event, details: observed.append((event, details)))
                          if observed is not None else None)
    result.configure_dispatch_eligibility(evidence.inspect, evidence.guard)
    return result, broker


def phase(storage, trade="trade-1"):
    return storage.connection.execute("SELECT phase FROM trades WHERE trade_id=?", (trade,)).fetchone()[0]


def outbox(storage, command):
    return storage.connection.execute("SELECT status FROM outbox WHERE command_id=?", (command,)).fetchone()[0]


def reservations(storage):
    return dict(storage.connection.execute("SELECT order_id, status FROM reservations").fetchall())


def held_entry(storage, evidence, observed=None):
    trade_manager, broker = manager(storage, evidence, observed)
    assert trade_manager.submit_plan(plan(), OpenTrade("open-1", "trade-1", 0, "entry", 10))
    reserve(storage, "trade-1", "open-1", 770)
    trade_manager.dispatch(STEP)                                 # шаг создания: данные не подтверждены
    assert outbox(storage, "open-1") == "PENDING"
    return trade_manager, broker


def test_held_entry_is_cancelled_on_next_step_without_waiting_for_data(tmp_path):
    observed = []
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, broker = held_entry(storage, Evidence(HOLD), observed)

        events = trade_manager.cancel_undelivered_increases(NEXT, NEXT)

        assert [(event.status, event.reason) for event in events] == [(ExecutionStatus.CANCEL, "entry-undelivered")]
        assert phase(storage) == "CANCELLED"
        assert reservations(storage) == {"open-1": "RELEASED"}
        assert outbox(storage, "open-1") == "UNDELIVERED"
        assert broker.actions == []
        assert observed == []                       # брокер заявку не получал: уведомления нет
        assert REASON_LABELS["entry-undelivered"] == "вход не доставлен: данные не подтверждены вовремя"
        CsvExporter(storage.connection, tmp_path / "trade_event.csv", tmp_path / "trade_summary.csv").export()
        rows = list(csv.DictReader((tmp_path / "trade_event.csv").open(encoding="utf-8")))
        assert rows[-1]["Причина"] == "вход не доставлен: данные не подтверждены вовремя"


def test_command_of_current_step_is_not_cancelled(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, _ = held_entry(storage, Evidence(HOLD))

        assert trade_manager.cancel_undelivered_increases(STEP, STEP) == ()

        assert phase(storage) == "ENTRY_PENDING" and outbox(storage, "open-1") == "PENDING"


def test_entry_delivered_in_its_step_is_left_to_ack_timeout(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, broker = manager(storage, Evidence(READY))
        trade_manager.submit_plan(plan(), OpenTrade("open-1", "trade-1", 0, "entry", 10))
        trade_manager.dispatch(STEP)
        assert outbox(storage, "open-1") == "SENT"

        assert trade_manager.cancel_undelivered_increases(NEXT, NEXT) == ()
        assert [action.command_id for action in broker.actions] == ["open-1"]


def test_held_add_is_cancelled_and_position_stays_intact(tmp_path):
    evidence = Evidence(READY)
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, broker = manager(storage, evidence)
        trade_manager.submit_plan(plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2),
                                  price_step=Decimal(1), step_cost=Decimal(1))
        trade_manager.dispatch(STEP)                              # вход исполнен
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop='96' WHERE trade_id='trade-1'")
        before, = trade_manager.restore()
        evidence.value = HOLD
        trade_manager.submit_action(AddToTrade("add-1", "trade-1", before.state.state_revision, "add", 3,
                                               "limit", Decimal(101)))
        trade_manager.dispatch(STEP)                              # добор удержан
        assert reservations(storage).get("add-1") == "ACTIVE"

        trade_manager.cancel_undelivered_increases(NEXT, NEXT)

        after, = trade_manager.restore()
        assert phase(storage) == before.state.phase.value == "OPEN"
        assert after.state.quantity == before.state.quantity == 2
        assert after.state.average_price == before.state.average_price
        assert after.plan.stop_price == before.plan.stop_price and after.plan.targets == before.plan.targets
        assert reservations(storage)["add-1"] == "RELEASED"
        assert outbox(storage, "add-1") == "UNDELIVERED"
        assert [action.command_id for action in broker.actions] == ["open-1"]


def test_held_close_is_not_an_increase_and_stays_pending(tmp_path):
    evidence = Evidence(READY)
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, _ = manager(storage, evidence)
        trade_manager.submit_plan(plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2))
        trade_manager.dispatch(STEP)
        recovered, = trade_manager.restore()
        evidence.value = HOLD
        trade_manager.submit_action(CloseTrade("close-1", "trade-1", recovered.state.state_revision, "signal-exit"))
        trade_manager.dispatch(STEP)

        assert trade_manager.cancel_undelivered_increases(NEXT, NEXT) == ()
        assert outbox(storage, "close-1") == "PENDING" and phase(storage) == "OPEN"


def test_interrupted_cancellation_is_finished_on_next_step(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, _ = held_entry(storage, Evidence(HOLD))
        assert storage.mark_outbox_undelivered(["open-1"]) == ("open-1",)   # сбой до CANCEL
        assert phase(storage) == "ENTRY_PENDING"

        trade_manager.cancel_undelivered_increases(NEXT, NEXT)
        assert trade_manager.cancel_undelivered_increases(NEXT, NEXT) == ()  # повтор ничего не делает

        assert phase(storage) == "CANCELLED" and reservations(storage) == {"open-1": "RELEASED"}
        assert storage.connection.execute("SELECT COUNT(*) FROM events WHERE command_id='open-1'").fetchone()[0] == 1


def test_claimed_command_is_not_cancelled_and_cancelled_one_is_never_delivered(tmp_path):
    evidence = Evidence(HOLD)
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trade_manager, broker = held_entry(storage, evidence)
        trade_manager.submit_plan(plan("trade-2", "binding-2"), OpenTrade("open-2", "trade-2", 0, "entry", 1))
        storage.connection.execute("UPDATE outbox SET status='CLAIMED' WHERE command_id='open-2'")
        storage.connection.commit()                               # доставка успела взять open-2

        trade_manager.cancel_undelivered_increases(NEXT, NEXT)
        assert outbox(storage, "open-2") == "CLAIMED" and phase(storage, "trade-2") == "ENTRY_PENDING"

        evidence.value = READY                                    # даже с доказанной актуальностью
        storage.requeue_claimed_outbox()
        trade_manager.dispatch(NEXT)
        assert "open-1" not in [action.command_id for action in broker.actions]
        assert outbox(storage, "open-1") == "UNDELIVERED"


def test_naive_journal_time_is_compared_as_utc():
    since = datetime(2026, 10, 14, 4, 23, tzinfo=timezone.utc)
    assert created_before_step("2026-10-13T12:00:00", since)
    assert not created_before_step("2026-10-14T04:23:00", since)
    assert not created_before_step("2026-10-13T12:00:00", None)
