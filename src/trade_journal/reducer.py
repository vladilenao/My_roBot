"""Atomic application of confirmed broker execution events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, calculation_trace


_INCREASE_ACTIONS = {"OPEN", "ADD"}
_REDUCE_ACTIONS = {"REDUCE", "CLOSE", "TARGET", "STOP"}
def _decimal(value: str | Decimal) -> Decimal:
    return Decimal(value)


def _text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class PositionState:
    quantity: int
    average_price: Decimal | None
    realized_pnl: Decimal
    fees: Decimal

    @property
    def net_realized_pnl(self) -> Decimal:
        """Realized result after every entry, add, and exit commission."""
        return self.realized_pnl - self.fees


@dataclass(frozen=True)
class AccountState:
    balance: Decimal
    equity: Decimal
    realized_pnl: Decimal
    fees: Decimal

    @property
    def net_realized_pnl(self) -> Decimal:
        """Keep net PnL separate from the gross realized trading result."""
        return self.realized_pnl - self.fees


def apply_fill(
    position: PositionState,
    account: AccountState,
    *,
    side: str,
    action_type: str,
    quantity: int,
    price: Decimal,
    fee: Decimal,
) -> tuple[PositionState, AccountState]:
    """Pure state transition for one incremental confirmed fill."""
    if quantity <= 0:
        raise ValueError("fill quantity must be positive")
    if price <= 0 or fee < 0:
        raise ValueError("fill price must be positive and fee cannot be negative")

    action = action_type.upper().split(":", 1)[0]
    if action in _INCREASE_ACTIONS:
        average = (
            price
            if position.quantity == 0
            else (position.average_price * position.quantity + price * quantity)
            / (position.quantity + quantity)
        )
        next_position = PositionState(
            quantity=position.quantity + quantity,
            average_price=average,
            realized_pnl=position.realized_pnl,
            fees=position.fees + fee,
        )
        gross = Decimal("0")
    elif action in _REDUCE_ACTIONS:
        if position.average_price is None or quantity > position.quantity:
            raise ValueError("reducing fill exceeds open position")
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        gross = direction * (price - position.average_price) * quantity
        remaining = position.quantity - quantity
        next_position = PositionState(
            quantity=remaining,
            average_price=position.average_price if remaining else None,
            realized_pnl=position.realized_pnl + gross,
            fees=position.fees + fee,
        )
    else:
        raise ValueError(f"unsupported filled action type {action_type!r}")

    next_account = AccountState(
        balance=account.balance + gross - fee,
        equity=account.equity + gross - fee,
        realized_pnl=account.realized_pnl + gross,
        fees=account.fees + fee,
    )
    return next_position, next_account


def apply_fill_with_trace(
    position: PositionState, account: AccountState, **kwargs: object
) -> tuple[PositionState, AccountState, CalculationTrace]:
    """Apply PnL/fees locally and retain all inputs required to recalculate it."""
    next_position, next_account = apply_fill(position, account, **kwargs)
    side, action = str(kwargs["side"]), str(kwargs["action_type"])
    quantity, price, fee = int(kwargs["quantity"]), kwargs["price"], kwargs["fee"]
    assert isinstance(price, Decimal) and isinstance(fee, Decimal)
    gross = next_account.realized_pnl - account.realized_pnl
    trace = calculation_trace(
        "clearing.apply_fill", inputs={
            "side": MeasuredValue(side, "side"), "action": MeasuredValue(action, "action"),
            "quantity": MeasuredValue(quantity, "contracts"), "fill_price": MeasuredValue(price, "price"),
            "average_price_before": MeasuredValue(position.average_price, "price"),
            "fee": MeasuredValue(fee, "RUB"), "gross_pnl_before": MeasuredValue(position.realized_pnl, "RUB"),
            "fees_before": MeasuredValue(position.fees, "RUB"),
        }, result=MeasuredValue({"quantity": next_position.quantity, "gross_pnl": str(gross),
                                  "fees": str(next_position.fees), "net_pnl": str(next_position.net_realized_pnl),
                                  "balance": str(next_account.balance)}, "position-account-state"),
        reason="confirmed-fill-cleared",
        formula="increase: weighted average; reduce: direction*(fill-average)*quantity; balance += gross-fee",
        links=TraceLinks(),
    )
    return next_position, next_account, trace


class ExecutionReducer:
    """Applies one broker outcome and its dependent state in a single transaction."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def apply(self, event: ExecutionEvent) -> bool:
        """Apply an event once; return ``False`` when its execution was already seen."""
        with self._storage.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM fills WHERE execution_id = ? UNION SELECT 1 FROM events WHERE event_id = ?",
                (event.execution_id, event.execution_id),
            ).fetchone():
                return False

            order = self._order(connection, event)
            self._assert_event_matches_order(event, order)
            now = event.timestamp.isoformat()
            status = event.status.value.upper()
            action_type = order["action_type"]
            filled = event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}

            if filled:
                position, account = self._states(connection, event.trade_id)
                next_position, next_account = apply_fill(
                    position,
                    account,
                    side=connection.execute(
                        "SELECT side FROM trades WHERE trade_id = ?", (event.trade_id,)
                    ).fetchone()[0],
                    action_type=action_type,
                    quantity=event.filled_quantity,
                    price=event.price,
                    fee=event.fee,
                )
                total_filled = order["filled_quantity"] + event.filled_quantity
                if total_filled > order["quantity"]:
                    raise ValueError("filled quantity exceeds order quantity")
                order_status = "FILLED" if total_filled == order["quantity"] else "PARTIAL"
                self._write_states(connection, event.trade_id, next_position, next_account, now)
                connection.execute(
                    "INSERT INTO fills (fill_id, order_id, trade_id, command_id, execution_id, quantity, price, fee, executed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event.execution_id, order["order_id"], event.trade_id, event.command_id,
                     event.execution_id, event.filled_quantity, _text(event.price), _text(event.fee), now),
                )
                connection.execute(
                    "UPDATE orders SET filled_quantity = ?, status = ?, updated_at = ? WHERE order_id = ?",
                    (total_filled, order_status, now, order["order_id"]),
                )
                self._update_reservation(connection, order, total_filled, now)
                self._update_target(connection, event.trade_id, action_type, event.filled_quantity)
                self._update_phase(connection, event.trade_id, action_type, next_position.quantity)
            else:
                order_status = status
                connection.execute(
                    "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
                    (order_status, now, order["order_id"]),
                )
                if event.status in {ExecutionStatus.REJECT, ExecutionStatus.CANCEL}:
                    self._release_reservation(connection, order, now)

            connection.execute(
                "INSERT INTO events (event_id, trade_id, order_id, command_id, event_type, payload_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.execution_id, event.trade_id, order["order_id"], event.command_id,
                 status, json.dumps(self._payload(event), sort_keys=True), now),
            )
        return True

    @staticmethod
    def _order(connection, event: ExecutionEvent) -> dict[str, object]:
        cursor = connection.execute(
            "SELECT * FROM orders WHERE command_id = ?", (event.command_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"unknown command {event.command_id}")
        return dict(zip((column[0] for column in cursor.description), row, strict=True))

    @staticmethod
    def _assert_event_matches_order(event: ExecutionEvent, order: dict[str, object]) -> None:
        if order["trade_id"] != event.trade_id:
            raise ValueError("execution trade does not match order")
        if event.order_id is not None and event.order_id != order["order_id"]:
            raise ValueError("execution order does not match command")

    @staticmethod
    def _states(connection, trade_id: str) -> tuple[PositionState, AccountState]:
        position_cursor = connection.execute("SELECT * FROM positions WHERE trade_id = ?", (trade_id,))
        account_cursor = connection.execute("SELECT * FROM account WHERE account_id = 1")
        position_row = position_cursor.fetchone()
        account_row = account_cursor.fetchone()
        position = dict(zip((column[0] for column in position_cursor.description), position_row, strict=True)) if position_row else None
        account = dict(zip((column[0] for column in account_cursor.description), account_row, strict=True)) if account_row else None
        if position is None or account is None:
            raise ValueError("position and account must exist before applying a fill")
        return (
            PositionState(position["quantity"], _decimal(position["average_price"]) if position["average_price"] else None,
                          _decimal(position["realized_pnl"]), _decimal(position["fees"])),
            AccountState(*(_decimal(account[key]) for key in ("balance", "equity", "realized_pnl", "fees"))),
        )

    @staticmethod
    def _write_states(connection, trade_id: str, position: PositionState, account: AccountState, now: str) -> None:
        connection.execute(
            "UPDATE positions SET quantity=?, average_price=?, realized_pnl=?, fees=?, net_realized_pnl=?, updated_at=? WHERE trade_id=?",
            (position.quantity, _text(position.average_price) if position.average_price is not None else None,
              _text(position.realized_pnl), _text(position.fees), _text(position.net_realized_pnl), now, trade_id),
        )
        connection.execute(
            "UPDATE account SET balance=?, equity=?, realized_pnl=?, fees=?, net_realized_pnl=?, updated_at=? WHERE account_id=1",
            (_text(account.balance), _text(account.equity), _text(account.realized_pnl), _text(account.fees),
             _text(account.net_realized_pnl), now),
        )

    @staticmethod
    def _update_reservation(connection, order: dict[str, object], filled: int, now: str) -> None:
        cursor = connection.execute("SELECT * FROM reservations WHERE order_id = ?", (order["order_id"],))
        row = cursor.fetchone()
        if row is None:
            return
        reservation = dict(zip((column[0] for column in cursor.description), row, strict=True))
        if filled < order["filled_quantity"] or filled > order["quantity"]:
            raise ValueError("invalid cumulative filled quantity for reservation")
        remaining = order["quantity"] - filled
        status = "RELEASED" if remaining == 0 else "ACTIVE"
        connection.execute(
            "UPDATE reservations SET risk_amount=?, margin_amount=?, status=?, updated_at=? WHERE reservation_id=?",
            (_text(_decimal(reservation["original_risk_amount"]) * remaining / order["quantity"]),
             _text(_decimal(reservation["original_margin_amount"]) * remaining / order["quantity"]),
             status, now, reservation["reservation_id"]),
        )

    @staticmethod
    def _release_reservation(connection, order: dict[str, object], now: str) -> None:
        """Release only the unfilled remainder after a broker-confirmed terminal outcome."""
        cursor = connection.execute("SELECT reservation_id FROM reservations WHERE order_id = ?", (order["order_id"],))
        row = cursor.fetchone()
        if row is not None:
            connection.execute(
                "UPDATE reservations SET risk_amount='0', margin_amount='0', status='RELEASED', updated_at=? "
                "WHERE reservation_id=?",
                (now, row[0]),
            )

    @staticmethod
    def _update_target(connection, trade_id: str, action_type: str, quantity: int) -> None:
        action, _, target_id = action_type.partition(":")
        if action != "TARGET" or not target_id:
            return
        cursor = connection.execute(
            "SELECT * FROM targets WHERE trade_id = ? AND target_id = ?", (trade_id, target_id)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"unknown target {target_id}")
        target = dict(zip((column[0] for column in cursor.description), row, strict=True))
        filled = target["filled_quantity"] + quantity
        if filled > target["planned_quantity"]:
            raise ValueError("target fill exceeds planned quantity")
        connection.execute(
            "UPDATE targets SET filled_quantity=?, status=? WHERE target_id=?",
            (filled, "FILLED" if filled == target["planned_quantity"] else "PARTIAL", target_id),
        )

    @staticmethod
    def _update_phase(connection, trade_id: str, action_type: str, quantity: int) -> None:
        """Advance lifecycle state only after a confirmed quantity has changed."""
        action = action_type.upper().split(":", 1)[0]
        if action == "OPEN" and quantity > 0:
            phase = "OPEN"
        elif action == "ADD" and quantity > 0:
            phase = "BUILDING"
        elif action in {"REDUCE", "TARGET"}:
            phase = "REDUCING" if quantity > 0 else "CLOSED"
        elif action in {"CLOSE", "STOP"} and quantity == 0:
            phase = "CLOSED"
        else:
            return
        connection.execute(
            "UPDATE trades SET phase = ?, state_revision = state_revision + 1 WHERE trade_id = ? AND phase != ?",
            (phase, trade_id, phase),
        )

    @staticmethod
    def _payload(event: ExecutionEvent) -> dict[str, str | int | None]:
        return {
            "fee": _text(event.fee), "price": _text(event.price) if event.price is not None else None,
            "reason": event.reason, "status": event.status.value, "quantity": event.filled_quantity,
        }
