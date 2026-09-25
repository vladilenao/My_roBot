"""Atomic application of confirmed broker execution events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.audit import CalculationTrace, CalculationTraceRepository, MeasuredValue, TraceLinks, calculation_trace


_INCREASE_ACTIONS = {"OPEN", "ADD"}
_REDUCE_ACTIONS = {"REDUCE", "CLOSE", "TARGET", "STOP"}
_TERMINAL_PHASES = ("CLOSED", "CANCELLED", "REJECTED", "ERROR")
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
    price_step: Decimal | None = None,
    step_cost: Decimal | None = None,
) -> tuple[PositionState, AccountState]:
    """Pure state transition for one incremental confirmed fill.

    Для сделок-снапшотов (``price_step`` и ``step_cost`` не ``None``) выходной
    PnL считается в рублях через стоимость шага; без снапшота сохраняется
    прежний сырой расчёт ``цена × объём`` (fallback, как у брокера).
    """
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
        delta = price - position.average_price
        if price_step is not None and step_cost is not None and price_step > 0:
            gross = direction * delta / price_step * step_cost * quantity
        else:
            gross = direction * delta * quantity
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
    price_step = kwargs.get("price_step")
    step_cost = kwargs.get("step_cost")
    trace = calculation_trace(
        "clearing.apply_fill", inputs={
            "side": MeasuredValue(side, "side"), "action": MeasuredValue(action, "action"),
            "quantity": MeasuredValue(quantity, "contracts"), "fill_price": MeasuredValue(price, "price"),
            "average_price_before": MeasuredValue(position.average_price, "price"),
            "price_step": MeasuredValue(price_step, "price") if price_step is not None
            else MeasuredValue("", "price"),
            "step_cost": MeasuredValue(step_cost, "RUB") if step_cost is not None
            else MeasuredValue("", "RUB"),
            "fee": MeasuredValue(fee, "RUB"), "gross_pnl_before": MeasuredValue(position.realized_pnl, "RUB"),
            "fees_before": MeasuredValue(position.fees, "RUB"),
        }, result=MeasuredValue({"quantity": next_position.quantity, "gross_pnl": str(gross),
                                  "fees": str(next_position.fees), "net_pnl": str(next_position.net_realized_pnl),
                                  "balance": str(next_account.balance)}, "position-account-state"),
        reason="confirmed-fill-cleared",
        formula=("increase: weighted average; "
                 "reduce: direction*(fill-average)/price_step*step_cost*quantity (raw fallback without factors); "
                 "balance += gross-fee"),
        links=TraceLinks(),
    )
    return next_position, next_account, trace


class ExecutionReducer:
    """Applies one broker outcome and its dependent state in a single transaction."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._traces = CalculationTraceRepository(storage)

    def apply(self, event: ExecutionEvent) -> bool:
        """Apply an event once; return ``False`` when its execution was already seen."""
        with self._storage.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM fills WHERE execution_id = ? UNION SELECT 1 FROM events WHERE event_id = ?",
                (event.execution_id, event.execution_id),
            ).fetchone():
                return False

            if self._is_broker_initiated(event.command_id):
                self._ensure_pv_order(connection, event)
            order = self._order(connection, event)
            self._assert_event_matches_order(event, order)
            now = event.timestamp.isoformat()
            status = event.status.value.upper()
            action_type = order["action_type"]
            filled = event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}

            if filled:
                position, account = self._states(connection, event.trade_id)
                trade_row = connection.execute(
                    "SELECT side, price_step, step_cost FROM trades WHERE trade_id = ?",
                    (event.trade_id,),
                ).fetchone()
                next_position, next_account, fill_trace = apply_fill_with_trace(
                    position,
                    account,
                    side=trade_row[0],
                    action_type=action_type,
                    quantity=event.filled_quantity,
                    price=event.price,
                    fee=event.fee,
                    price_step=Decimal(str(trade_row[1])) if trade_row[1] is not None else None,
                    step_cost=Decimal(str(trade_row[2])) if trade_row[2] is not None else None,
                )
                self._traces.record_in_transaction(connection, fill_trace)
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
                self._release_reservations_if_terminal(connection, event.trade_id, now)
            else:
                order_status = status
                connection.execute(
                    "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
                    (order_status, now, order["order_id"]),
                )
                if event.status in {ExecutionStatus.REJECT, ExecutionStatus.CANCEL}:
                    self._release_reservation(connection, order, now)
                    self._update_terminal_outcome(connection, event, order, now)
                    self._release_reservations_if_terminal(connection, event.trade_id, now)

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
    def _is_broker_initiated(command_id: str) -> bool:
        """Защитные закрытия брокера помечены маркером ``:pv:`` в command_id."""
        return ":pv:" in command_id

    def _ensure_pv_order(self, connection, event: ExecutionEvent) -> None:
        """Синтезировать durable-заявку для закрытия, не порождённого командой.

        Схема связывает каждый филл с заявкой и каждую заявку с командой, но у
        защитного закрытия команды менеджера нет. Чтобы применять его штатным
        путём филла, создаётся пара ``outbox('SENT')`` + ``orders`` с типом
        ``STOP`` или ``TARGET:<id>``. Статус ``SENT`` исключает повторный
        диспатч ``claim_outbox`` (берёт только ``PENDING``). Повторный реплей
        того же события до синтеза отсекается проверкой ``execution_id`` выше.
        """
        if connection.execute(
            "SELECT 1 FROM orders WHERE command_id = ?", (event.command_id,)
        ).fetchone():
            return
        action_type, _, target_id = self._pv_action(event)
        now = event.timestamp.isoformat()
        connection.execute(
            "INSERT INTO outbox (command_id, trade_id, payload_json, status, created_at, sent_at) "
            "VALUES (?, ?, ?, 'SENT', ?, ?)",
            (event.command_id, event.trade_id, "{}", now, now),
        )
        connection.execute(
            "INSERT INTO orders (order_id, trade_id, command_id, action_type, status, quantity, "
            "filled_quantity, requested_price, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'PENDING', ?, 0, NULL, ?, ?)",
            (event.command_id, event.trade_id, event.command_id, action_type,
             event.filled_quantity, now, now),
        )
        if target_id:
            self._bind_target_plan(connection, event.trade_id, target_id)

    @staticmethod
    def _pv_action(event: ExecutionEvent) -> tuple[str, str, str]:
        """Разобрать ``<trade>:pv:<stop|tp>[:<target>]:<bar>`` в (action, kind, target)."""
        _, _, rest = event.command_id.partition(":pv:")
        kind, _, payload = rest.partition(":")
        if kind == "stop":
            return "STOP", kind, ""
        if kind == "tp":
            target_id = payload.partition(":")[0]
            return f"TARGET:{target_id}", kind, target_id
        raise ValueError(f"unsupported protective directive {kind!r}")

    @staticmethod
    def _bind_target_plan(connection, trade_id: str, target_id: str) -> None:
        """Заполнить ``planned_quantity`` цели из подтверждённых входов.

        Профильные планы не знают объём цели заранее: он зависит от накопленных
        входов. Защитное закрытие цели применяется через ``_update_target``,
        который сравнивает заполнение с ``planned_quantity`` — её нужно связать
        из суммы исполнений ``OPEN``/``ADD`` и долей цели по плану (та же
        аллокация, что у брокера: последняя цель получает остаток).
        """
        row = connection.execute(
            "SELECT planned_quantity FROM targets WHERE trade_id = ? AND target_id = ?",
            (trade_id, target_id),
        ).fetchone()
        if row is None or row[0] != 0:
            return
        entry_quantity = connection.execute(
            "SELECT COALESCE(SUM(f.quantity), 0) FROM fills f "
            "JOIN orders o ON o.order_id = f.order_id "
            "WHERE f.trade_id = ? AND o.action_type IN ('OPEN', 'ADD')",
            (trade_id,),
        ).fetchone()[0]
        if entry_quantity <= 0:
            return
        plan_row = connection.execute(
            "SELECT plan_json FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        shares = {
            item["target_id"]: Decimal(str(item["share"]))
            for item in json.loads(plan_row[0]).get("targets", [])
        }
        ids = [t[0] for t in connection.execute(
            "SELECT target_id FROM targets WHERE trade_id = ? ORDER BY target_index", (trade_id,)
        ).fetchall()]
        allocated = 0
        planned = 0
        for index, item in enumerate(ids):
            quantity = (
                entry_quantity - allocated
                if index == len(ids) - 1
                else int(entry_quantity * shares.get(item, Decimal("0")))
            )
            allocated += quantity
            if item == target_id:
                planned = quantity
                break
        if planned > 0:
            connection.execute(
                "UPDATE targets SET planned_quantity = ? WHERE trade_id = ? AND target_id = ?",
                (planned, trade_id, target_id),
            )

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
    def _release_reservations_if_terminal(connection, trade_id: str, now: str) -> None:
        """Free the whole risk budget once the trade reached a terminal phase.

        :meth:`_release_reservation` only frees the remainder of the very order
        the event refers to.  An unfilled entry is cancelled through a separate
        ``CANCEL`` command, so its entry reservation is never touched there and
        would keep blocking every later admission.
        """
        row = connection.execute(
            "SELECT phase FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if row is None or row[0] not in _TERMINAL_PHASES:
            return
        connection.execute(
            "UPDATE reservations SET risk_amount='0', margin_amount='0', status='RELEASED', updated_at=? "
            "WHERE trade_id = ? AND status = 'ACTIVE'",
            (now, trade_id),
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
    def _update_terminal_outcome(connection, event: ExecutionEvent, order: dict[str, object], now: str) -> None:
        """Keep rejected broker requests distinct from user cancellations."""
        action = str(order["action_type"]).upper().split(":", 1)[0]
        if event.status is ExecutionStatus.REJECT and action == "OPEN":
            phase = "REJECTED"
        elif action in {"OPEN", "CANCEL"}:
            quantity = connection.execute(
                "SELECT quantity FROM positions WHERE trade_id = ?", (event.trade_id,)
            ).fetchone()
            if quantity and quantity[0] > 0:
                return
            phase = "CANCELLED"
        else:
            return
        connection.execute(
            "UPDATE trades SET phase = ?, state_revision = state_revision + 1, updated_at = ? "
            "WHERE trade_id = ? AND phase NOT IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR')",
            (phase, now, event.trade_id),
        )

    @staticmethod
    def _payload(event: ExecutionEvent) -> dict[str, str | int | None]:
        return {
            "fee": _text(event.fee), "price": _text(event.price) if event.price is not None else None,
            "reason": event.reason, "status": event.status.value, "quantity": event.filled_quantity,
            "low": _text(event.market_low) if event.market_low is not None else None,
            "high": _text(event.market_high) if event.market_high is not None else None,
        }
