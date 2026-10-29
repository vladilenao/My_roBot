"""Сверка сроков ожидающих входов перед допуском новой заявки (expire-pending-entries-before-admission).

Настоящие ``Storage``, reducer и ``TradeManager``; брокер — подставной. Просроченный
вход любого инструмента снимается до расчёта нового входа, а локальное снятие
неотправленной заявки не доходит до наблюдателя уведомлений.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.portfolio.models import ContractMeta
from src.strategies.contracts import SignalType
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import CancelEntry, OpenTrade
from src.trade_management.manager import DispatchDataEvidence, SignalAdmission, TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

T0 = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
READY = DispatchDataEvidence(True, True, True)
NO_EXECUTION = DispatchDataEvidence(False, True, True)
HOLD = DispatchDataEvidence(False, False, None)


class AckBroker(BrokerPort):
    """Вход принимается (ACK) и не исполняется; отмена подтверждается."""

    def __init__(self):
        self.actions = []

    def register_trade(self, plan):
        pass

    def contract_for(self, ticker):
        return ContractMeta(ticker=ticker, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0,
                            expiration_date=None)

    def submit(self, action, now):
        self.actions.append(action)
        status = ExecutionStatus.CANCEL if isinstance(action, CancelEntry) else ExecutionStatus.ACK
        return ExecutionEvent(f"{action.command_id}:{status.value}", action.command_id, action.command_id,
                              action.trade_id, status, 0, None, Decimal(0), now, action.reason)


class Env:
    def __init__(self, storage, evidence=READY, *, live=True):
        self.storage = storage
        self.now = T0
        self.evidence = evidence
        self.lock = RLock()
        self.broker = AckBroker()
        self.observed = []
        self.manager = TradeManager(storage, self.broker, initial_balance=Decimal(100000),
                                    clock=lambda: self.now,
                                    execution_observer=lambda event, details: self.observed.append(event))
        if live:
            self.manager.configure_dispatch_eligibility(lambda *args: self.evidence, self.guard)
        self.planned = []                                  # состояние журнала в момент расчёта нового входа
        self.manager._plan_entry = self._plan_entry

    @contextmanager
    def guard(self):
        with self.lock:
            yield

    def _plan_entry(self, *args, **kwargs):
        self.planned.append(self.snapshot())
        return SignalAdmission()

    def snapshot(self):
        phases = dict(self.storage.connection.execute("SELECT trade_id, phase FROM trades").fetchall())
        reservations = dict(self.storage.connection.execute("SELECT order_id, status FROM reservations").fetchall())
        return phases, reservations

    def enter(self, uid, timeframe="1h", risk=800):
        plan = TradePlan(f"trade-{uid}", f"binding-{uid}", uid, "BUY", f"signal-{uid}", Decimal(100), Decimal(96),
                         (TargetPlan("tp", Decimal(104), Decimal(1)),), ProfileSnapshot("levels_rr", "1", {}),
                         T0, timeframe)
        assert self.manager.submit_plan(plan, OpenTrade(f"open-{uid}", f"trade-{uid}", 0, "entry", 1))
        self.storage.reserve_candidates((ReservationCandidate(
            reservation_id=f"reservation-{uid}", trade_id=f"trade-{uid}", order_id=f"open-{uid}", priority=0,
            assignment_id=f"binding-{uid}", instrument_id=uid, signal_id=f"signal-{uid}",
            risk_amount=Decimal(risk), margin_amount=Decimal(0)),),
            risk_budget=Decimal(1900), margin_budget=Decimal(1900))
        self.manager.dispatch(self.now)

    def admit(self, uid="B"):
        return self.manager.actions_for_signal(
            SimpleNamespace(id=f"binding-{uid}", timeframe="1h"), SimpleNamespace(signal_type=SignalType.BUY),
            SimpleNamespace(ticker=uid), None, None, timeframe="1h")

    def outbox(self, command):
        return self.storage.connection.execute("SELECT status FROM outbox WHERE command_id=?", (command,)).fetchone()[0]

    def cancel_orders(self):
        return self.storage.connection.execute("SELECT COUNT(*) FROM orders WHERE action_type='CANCEL'").fetchone()[0]

    def cancelled_reasons(self):
        return [event.reason for event in self.observed if event.status is ExecutionStatus.CANCEL]


def test_expired_acked_entry_of_other_instrument_is_cancelled_before_planning(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage)
        env.enter("A")                                       # 1h → срок 4 часа, подтверждён в T0
        env.now = T0 + timedelta(hours=5)

        env.admit("B")

        (phases, reservations), = env.planned
        assert phases["trade-A"] == "CANCELLED" and reservations["open-A"] == "RELEASED"
        cancel = env.broker.actions[-1]
        assert isinstance(cancel, CancelEntry) and cancel.reason == "entry-timeout"
        assert env.cancelled_reasons() == ["entry-timeout"]       # подтверждённая отмена уведомляется


def test_entry_younger_than_ttl_is_kept(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage)
        env.enter("A")
        env.now = T0 + timedelta(hours=3)

        env.admit("B")

        (phases, reservations), = env.planned
        assert phases["trade-A"] == "ENTRY_PENDING" and reservations["open-A"] == "ACTIVE"
        assert [type(action) for action in env.broker.actions] == [OpenTrade]


def test_acked_entry_is_kept_while_execution_is_not_ready(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage)
        env.enter("A")
        env.now = T0 + timedelta(hours=5)
        env.evidence = NO_EXECUTION                           # исполнение не догнало минуты простоя

        env.admit("B")

        (phases, reservations), = env.planned
        assert phases["trade-A"] == "ENTRY_PENDING" and reservations["open-A"] == "ACTIVE"
        cancel_id, = [row[0] for row in storage.connection.execute(
            "SELECT command_id FROM orders WHERE action_type='CANCEL'")]
        assert env.outbox(cancel_id) == "PENDING"            # отмена ждёт допуска, а не засчитывается


def test_repeated_sweeps_do_not_create_second_cancel(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage)
        env.enter("A")
        env.now = T0 + timedelta(hours=5)
        env.evidence = NO_EXECUTION

        env.admit("B")
        env.admit("C")

        assert env.cancel_orders() == 1


def test_undelivered_entry_older_than_ttl_is_cancelled_locally_and_silently(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage, HOLD)
        env.enter("A")                                       # данные не подтверждены: команда осталась в очереди
        assert env.outbox("open-A") == "PENDING"
        env.now = T0 + timedelta(hours=5)

        env.admit("B")

        (phases, reservations), = env.planned
        assert phases["trade-A"] == "CANCELLED" and reservations["open-A"] == "RELEASED"
        assert env.outbox("open-A") == "UNDELIVERED"
        assert env.broker.actions == []
        journal = [json.loads(payload)["reason"] for payload, in storage.connection.execute(
            "SELECT payload_json FROM events WHERE command_id='open-A' AND event_type='CANCEL'")]
        assert journal == ["entry-undelivered"]               # в журнале есть
        assert env.cancelled_reasons() == []                  # уведомления нет


def test_undelivered_entry_younger_than_ttl_is_not_touched_by_sweep(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage, HOLD)
        env.enter("A")
        env.now = T0 + timedelta(hours=1)

        env.admit("B")

        (phases, _), = env.planned
        assert phases["trade-A"] == "ENTRY_PENDING" and env.outbox("open-A") == "PENDING"


def test_local_cancel_of_step_rule_is_silent_too(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage, HOLD)
        env.enter("A")

        env.manager.cancel_undelivered_increases(T0 + timedelta(minutes=1))

        assert env.cancelled_reasons() == []
        assert storage.connection.execute("SELECT phase FROM trades").fetchone()[0] == "CANCELLED"


def test_historical_mode_does_not_sweep(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(storage, live=False)
        env.enter("A")
        env.now = T0 + timedelta(hours=5)

        env.admit("B")

        (phases, _), = env.planned
        assert phases["trade-A"] == "ENTRY_PENDING"
        assert [type(action) for action in env.broker.actions] == [OpenTrade]
