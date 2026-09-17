"""Workflow coordinator for durable, addressed trade management."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import RecoveredTrade, ReservationCandidate, Storage
from src.trade_management.actions import (
    AddToTrade, CancelEntry, CloseTrade, MoveStop, OpenTrade, ReduceTrade, TradeAction,
)
from src.trade_management.models import TradePhase, TradePlan


_INCREASES = (OpenTrade, AddToTrade)
_MANAGEABLE = {TradePhase.OPEN, TradePhase.BUILDING, TradePhase.REDUCING}


class TradeManager:
    """Coordinates workflow intent without duplicating factual broker state.

    SQLite remains authoritative for plans, orders, and confirmed fills.  This
    class keeps no position cache; ``restore`` always reads the durable snapshot.
    """

    def __init__(
        self,
        storage: Storage,
        broker: BrokerPort,
        *,
        initial_balance: Decimal = Decimal("0"),
        post_fill_check: Callable[[ExecutionEvent], tuple[TradeAction, ...]] | None = None,
    ) -> None:
        self._storage = storage
        self._broker = broker
        self._reducer = ExecutionReducer(storage)
        self._initial_balance = initial_balance
        self._post_fill_check = post_fill_check

    def restore(self) -> tuple[RecoveredTrade, ...]:
        """Return pending and open trades solely from the SQLite snapshot."""
        recovered = self._storage.load_trades()
        register = getattr(self._broker, "register_trade", None)
        if callable(register):
            for trade in recovered:
                register(trade.plan)
        return recovered

    def submit_plan(
        self,
        plan: TradePlan,
        action: OpenTrade,
        *,
        reservation: ReservationCandidate | None = None,
        risk_budget: Decimal | None = None,
        margin_budget: Decimal | None = None,
    ) -> bool:
        """Persist a newly planned entry and its durable broker intent atomically."""
        if action.trade_id != plan.trade_id or action.state_revision != 0:
            raise ValueError("opening action must own the new plan at revision 0")
        if action.quantity <= 0:
            raise ValueError("opening quantity must be positive")
        if reservation is not None and reservation.order_id != action.command_id:
            raise ValueError("reservation order_id must equal the opening command_id")
        if (risk_budget is None) != (margin_budget is None):
            raise ValueError("risk and margin budgets must be provided together")
        now = plan.created_at.isoformat()
        with self._storage.transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM processed_signals WHERE assignment_id = ? AND signal_id = ?",
                (plan.assignment_id, plan.signal_id),
            ).fetchone()
            if duplicate:
                return False
            connection.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, 'ENTRY_PENDING', 0, '{}', ?, ?)",
                (plan.trade_id, plan.assignment_id, plan.instrument_id, plan.signal_id, plan.side,
                 json.dumps(_plan_payload(plan), sort_keys=True),
                 json.dumps(_profile_payload(plan), sort_keys=True), now, now),
            )
            connection.execute(
                "INSERT INTO positions VALUES (?, ?, 0, NULL, '0', '0', '0', ?)",
                (plan.trade_id, plan.side, now),
            )
            connection.execute(
                "INSERT INTO protection VALUES (?, NULL, NULL, NULL, NULL, ?)", (plan.trade_id, now),
            )
            for index, target in enumerate(plan.targets):
                connection.execute(
                    "INSERT INTO targets VALUES (?, ?, ?, ?, 0, 0, 'PENDING')",
                    (target.target_id, plan.trade_id, index, str(target.price)),
                )
            connection.execute(
                "INSERT INTO processed_signals VALUES (?, ?, ?, ?)",
                (plan.assignment_id, plan.signal_id, plan.trade_id, now),
            )
            self._insert_action(connection, action, now)
            self._ensure_account(connection, now)
            self._reserve(connection, reservation, risk_budget, margin_budget, now)
        register = getattr(self._broker, "register_trade", None)
        if callable(register):
            register(plan)
        return True

    def submit_action(self, action: TradeAction, *, assignment_id: str | None = None) -> bool:
        """Validate and durably queue one management action before broker delivery."""
        now = datetime.now(timezone.utc).isoformat()
        with self._storage.transaction() as connection:
            row = connection.execute(
                "SELECT assignment_id, phase, state_revision FROM trades WHERE trade_id = ?", (action.trade_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown trade")
            owner, phase, revision = row
            if assignment_id is not None and assignment_id != owner:
                raise ValueError("action does not belong to assignment")
            if action.state_revision != revision:
                raise ValueError("stale-state-revision")
            self._validate_phase(action, TradePhase(phase))
            if connection.execute("SELECT 1 FROM outbox WHERE command_id = ?", (action.command_id,)).fetchone():
                return False
            self._insert_action(connection, action, now)
            if isinstance(action, MoveStop):
                connection.execute(
                    "UPDATE protection SET pending_stop=?, pending_command_id=?, updated_at=? WHERE trade_id=?",
                    (str(action.stop_price), action.command_id, now, action.trade_id),
                )
        return True

    def dispatch(self, now: datetime, *, limit: int = 100) -> tuple[ExecutionEvent, ...]:
        """Deliver persisted outbox commands and immediately consume their outcomes."""
        events: list[ExecutionEvent] = []
        for command in self._storage.claim_outbox(limit):
            action = _action_from_payload(command.payload)
            event = self._broker.submit(action, now)
            self._storage.mark_outbox_sent(command.command_id, sent_at=now)
            self.consume(event)
            events.append(event)
        return tuple(events)

    def consume(self, event: ExecutionEvent) -> bool:
        """Reduce one broker event, then schedule post-fill risk/protection checks."""
        applied = self._reducer.apply(event)
        if not applied:
            return False
        if event.status is ExecutionStatus.ACK:
            self._confirm_stop(event)
        if event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL} and self._post_fill_check:
            for action in self._post_fill_check(event):
                self.submit_action(action)
        return True

    @staticmethod
    def _validate_phase(action: TradeAction, phase: TradePhase) -> None:
        if isinstance(action, OpenTrade):
            allowed = phase is TradePhase.PLANNED
        elif isinstance(action, AddToTrade):
            allowed = phase in {TradePhase.OPEN, TradePhase.BUILDING}
        elif isinstance(action, CancelEntry):
            allowed = phase is TradePhase.ENTRY_PENDING
        else:
            allowed = phase in _MANAGEABLE
        if not allowed:
            raise ValueError(f"action {type(action).__name__} is invalid in phase {phase}")

    def _insert_action(self, connection, action: TradeAction, now: str) -> None:
        payload = _action_payload(action)
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, ?, 'PENDING', ?, NULL)",
            (action.command_id, action.trade_id, json.dumps(payload, sort_keys=True), now),
        )
        quantity = action.quantity if isinstance(action, (OpenTrade, AddToTrade, ReduceTrade)) else 1
        if isinstance(action, CloseTrade):
            row = connection.execute(
                "SELECT quantity FROM positions WHERE trade_id = ?", (action.trade_id,)
            ).fetchone()
            if row is None or row[0] <= 0:
                raise ValueError("cannot close a trade without an open quantity")
            quantity = row[0]
        requested_price = str(action.stop_price) if isinstance(action, MoveStop) else None
        action_type = {
            OpenTrade: "OPEN",
            AddToTrade: "ADD",
            ReduceTrade: "REDUCE",
            CloseTrade: "CLOSE",
            MoveStop: "MOVESTOP",
            CancelEntry: "CANCEL",
        }[type(action)]
        if isinstance(action, ReduceTrade) and action.target_id:
            action_type = f"TARGET:{action.target_id}"
        connection.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, 'PENDING', ?, 0, ?, ?, ?)",
            (action.command_id, action.trade_id, action.command_id, action_type, quantity,
             requested_price, now, now),
        )

    def _reserve(self, connection, candidate, risk_budget, margin_budget, now: str) -> None:
        if candidate is None:
            return
        if risk_budget is None or margin_budget is None:
            raise ValueError("reservation requires risk and margin budgets")
        rows = connection.execute(
            "SELECT risk_amount, margin_amount FROM reservations WHERE status = 'ACTIVE'"
        ).fetchall()
        used_risk = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        used_margin = sum((Decimal(row[1]) for row in rows), Decimal("0"))
        if used_risk + candidate.risk_amount > risk_budget or used_margin + candidate.margin_amount > margin_budget:
            raise ValueError("risk-or-margin-budget")
        connection.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (candidate.reservation_id, candidate.trade_id, candidate.order_id,
             str(candidate.risk_amount), str(candidate.margin_amount), str(candidate.risk_amount),
             str(candidate.margin_amount), now, now),
        )

    def _ensure_account(self, connection, now: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO account VALUES (1, ?, ?, '0', '0', '0', ?)",
            (str(self._initial_balance), str(self._initial_balance), now),
        )

    def _confirm_stop(self, event: ExecutionEvent) -> None:
        with self._storage.transaction() as connection:
            row = connection.execute(
                "SELECT requested_price FROM orders WHERE command_id = ? AND action_type = 'MOVESTOP'",
                (event.command_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE protection SET confirmed_stop=?, pending_stop=NULL, confirmed_order_id=?, "
                    "pending_command_id=NULL, updated_at=? WHERE trade_id=?",
                    (row[0], event.order_id, event.timestamp.isoformat(), event.trade_id),
                )
                connection.execute(
                    "UPDATE trades SET state_revision = state_revision + 1, updated_at = ? WHERE trade_id = ?",
                    (event.timestamp.isoformat(), event.trade_id),
                )


def _profile_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "name": plan.profile.name,
        "version": plan.profile.version,
        "parameters": _json_value(dict(plan.profile.parameters)),
    }


def _plan_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "reference_entry": str(plan.reference_entry), "stop_price": str(plan.stop_price),
        "targets": [{"target_id": target.target_id, "share": str(target.share)} for target in plan.targets],
    }


def _action_payload(action: TradeAction) -> dict[str, object]:
    result = asdict(action)
    result["type"] = type(action).__name__
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in result.items()}


def _action_from_payload(payload: dict[str, object]) -> TradeAction:
    data = dict(payload)
    action_type = data.pop("type")
    if action_type == "MoveStop":
        data["stop_price"] = Decimal(str(data["stop_price"]))
    constructors = {
        "OpenTrade": OpenTrade, "AddToTrade": AddToTrade, "ReduceTrade": ReduceTrade,
        "CloseTrade": CloseTrade, "MoveStop": MoveStop, "CancelEntry": CancelEntry,
    }
    try:
        return constructors[str(action_type)](**data)
    except KeyError as error:
        raise ValueError(f"unknown action type {action_type!r}") from error


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
