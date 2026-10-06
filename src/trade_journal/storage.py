"""SQLite connection and transaction helpers for the trade journal."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
from decimal import Decimal
from typing import Callable, Iterator, Mapping

from src.trade_journal.schema import initialize_schema
from src.scheduler.clock import Clock, as_aware, as_clock
from src.trade_journal.export import AuditExporter, CsvExporter
from src.trade_management.models import (
    CURRENT_ALGORITHM,
    LEGACY_ALGORITHM,
    CostSnapshot,
    PlanEconomics,
    ProfileSnapshot,
    StopBasis,
    TargetPlan,
    TradePhase,
    TradePlan,
    TradeState,
)
from src.trade_management.actions import TradeAction, action_from_payload
from src.trade_management.audit import (
    CalculationTrace,
    CalculationTraceRepository,
    MeasuredValue,
    TraceLinks,
    TraceOutcome,
    calculation_trace,
)


BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class OutboxCommand:
    """A durable command intent ready to be submitted to a broker."""

    command_id: str
    trade_id: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class PendingIncrease:
    action: TradeAction
    submitted_at: datetime


@dataclass(frozen=True)
class PendingStop:
    action: TradeAction
    submitted_at: datetime


@dataclass(frozen=True)
class PendingExit:
    action: TradeAction
    submitted_at: datetime


@dataclass(frozen=True)
class RecoveredTrade:
    """The complete persisted management state for one trade."""

    plan: TradePlan
    state: TradeState
    entry_ack_at: datetime | None = None
    entry_quantity: int | None = None
    target_filled: Mapping[str, int] | None = None
    pending_increases: tuple[PendingIncrease, ...] = ()
    pending_stops: tuple[PendingStop, ...] = ()
    pending_exits: tuple[PendingExit, ...] = ()
    last_increase_at: datetime | None = None
    last_increase_price: Decimal | None = None
    last_observed_bar: datetime | None = None


@dataclass(frozen=True)
class ReservationCandidate:
    """A sized increase awaiting an atomic risk and margin reservation."""

    reservation_id: str
    trade_id: str
    order_id: str
    priority: int
    assignment_id: str
    instrument_id: str
    signal_id: str
    risk_amount: Decimal
    margin_amount: Decimal

    def __post_init__(self) -> None:
        if not all((self.reservation_id, self.trade_id, self.order_id, self.assignment_id,
                    self.instrument_id, self.signal_id)):
            raise ValueError("reservation, trade, order, assignment, instrument, and signal IDs are required")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")
        for name, amount in (("risk_amount", self.risk_amount), ("margin_amount", self.margin_amount)):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")


@dataclass(frozen=True)
class ReservationDecision:
    candidate: ReservationCandidate
    accepted: bool
    reason: str | None = None


def configure_connection(connection: sqlite3.Connection) -> None:
    """Apply required safety and concurrency settings to one connection."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def connect(database: str | Path, *, initial_deposit: str | None = None) -> sqlite3.Connection:
    """Open a database only after its schema is known to this application."""
    connection = sqlite3.connect(database)
    try:
        configure_connection(connection)
        initialize_schema(connection, initial_balance=initial_deposit)
    except BaseException:
        connection.close()
        raise
    return connection


class Storage:
    """Owns one SQLite connection and exposes explicit write transactions."""

    def __init__(
        self,
        database: str | Path,
        *,
        journal_path: str | Path | None = None,
        positions_path: str | Path | None = None,
        audit_path: str | Path | None = None,
        audit_max_bytes: int = 10_485_760,
        audit_backup_count: int = 5,
        initial_deposit: str | None = None,
        clock: Clock | Callable[[], datetime] | None = None,
    ) -> None:
        if (journal_path is None) != (positions_path is None):
            raise ValueError("journal and positions export paths must be configured together")
        if journal_path is not None:
            database_path = Path(database).resolve()
            for name, export_path in (("journal", journal_path), ("positions", positions_path)):
                if database_path == Path(export_path).resolve():
                    raise ValueError(f"database path must not match {name} export path")
        if audit_path is not None and Path(database).resolve() == Path(audit_path).resolve():
            raise ValueError("database path must not match audit export path")
        self.connection = connect(database, initial_deposit=initial_deposit)
        self._clock = as_clock(clock)
        self._trace_repository = CalculationTraceRepository(self)
        self._exporter = (
            CsvExporter(self.connection, Path(journal_path), Path(positions_path), names=self.instrument_names())
            if journal_path is not None else None
        )
        self._audit_exporter = (
            AuditExporter(
                self.connection,
                Path(audit_path),
                max_bytes=audit_max_bytes,
                backup_count=audit_backup_count,
            )
            if audit_path is not None else None
        )
        self.requeue_claimed_outbox()
        self.release_terminal_reservations()
        self.export()

    def close(self) -> None:
        if self._audit_exporter is not None:
            self._audit_exporter.close()
        self.connection.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _now(self) -> datetime:
        """Рыночный момент записи: виртуальные часы прогона либо системные часы."""
        return as_aware(self._clock.now())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # One writer serializes budget reads with reservation writes across processes.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
            self._mark_export_required()
            self.export()

    def export(self) -> bool:
        """Retry configured projections without affecting durable state."""
        csv_exported = self._exporter.export() if self._exporter is not None else True
        audit_exported = self._audit_exporter.export() if self._audit_exporter is not None else True
        return csv_exported and audit_exported

    def set_contract_metadata(self, contracts: Mapping[str, object]) -> None:
        """Hand broker contract metadata to the CSV card projection."""
        if self._exporter is None:
            return
        self._exporter.set_contracts(contracts)
        self.export()

    def set_names(self, names: Mapping[str, str]) -> None:
        """Hand ticker -> short contract name mapping to the CSV projections.

        The same mapping is persisted so that tools opening the database without
        a running bot can render short contract names.  The persisted map is not
        part of any projection, so saving it does not raise an export revision.

        Persisted names win for contracts missing from ``names``, so a run that
        selects a subset of instruments keeps short names for older trades
        instead of falling back to "контракт не указан".
        """
        merged = {**self.instrument_names(), **dict(names)}
        if self._exporter is not None:
            self._exporter.set_names(merged)
        self._save_instrument_names(names)
        self.export()

    def _save_instrument_names(self, names: Mapping[str, str]) -> None:
        rows = [
            (ticker, short_name)
            for ticker, short_name in names.items()
            if ticker and short_name
        ]
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT INTO instrument_names (ticker, short_name, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(ticker) DO UPDATE SET short_name = excluded.short_name, "
                "updated_at = excluded.updated_at",
                rows,
            )

    def instrument_names(self) -> dict[str, str]:
        """Read the persisted ticker -> short contract name mapping."""
        rows = self.connection.execute(
            "SELECT ticker, short_name FROM instrument_names"
        ).fetchall()
        return {ticker: short_name for ticker, short_name in rows}

    def _mark_export_required(self) -> None:
        if self._exporter is None and self._audit_exporter is None:
            return
        with self.connection:
            self.connection.execute(
                "UPDATE export_state SET required_revision = required_revision + 1, updated_at = datetime('now') "
                "WHERE export_id = 1"
            )

    def reserve_candidates(
        self,
        candidates: tuple[ReservationCandidate, ...],
        *,
        risk_budget: Decimal,
        margin_budget: Decimal,
        created_at: datetime | None = None,
    ) -> tuple[ReservationDecision, ...]:
        """Atomically reserve approved candidates in their stable portfolio order.

        The amounts are the already-sized incremental risk and margin.  Existing
        active reservations are included in the same transaction, so separate
        callers cannot each spend the same remaining budget.
        """
        for name, amount in (("risk_budget", risk_budget), ("margin_budget", margin_budget)):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if len({candidate.order_id for candidate in candidates}) != len(candidates):
            raise ValueError("candidates must not repeat an order")

        ordered = tuple(sorted(
            candidates,
            key=lambda candidate: (-candidate.priority, candidate.assignment_id,
                                   candidate.instrument_id, candidate.signal_id),
        ))
        now = (created_at or self._now()).isoformat()
        with self.transaction() as connection:
            active_reservations = connection.execute(
                "SELECT risk_amount, margin_amount FROM reservations WHERE status = 'ACTIVE'"
            ).fetchall()
            used_risk = sum((Decimal(row[0]) for row in active_reservations), Decimal("0"))
            used_margin = sum((Decimal(row[1]) for row in active_reservations), Decimal("0"))
            decisions: list[ReservationDecision] = []
            for candidate in ordered:
                existing = connection.execute(
                    "SELECT reservation_id FROM reservations WHERE order_id = ?",
                    (candidate.order_id,),
                ).fetchone()
                if existing is not None:
                    decisions.append(ReservationDecision(candidate, True))
                elif used_risk + candidate.risk_amount > risk_budget:
                    decisions.append(ReservationDecision(candidate, False, "risk-budget"))
                elif used_margin + candidate.margin_amount > margin_budget:
                    decisions.append(ReservationDecision(candidate, False, "margin-budget"))
                else:
                    connection.execute(
                        "INSERT INTO reservations (reservation_id, trade_id, order_id, risk_amount, "
                        "margin_amount, original_risk_amount, original_margin_amount, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
                        (candidate.reservation_id, candidate.trade_id, candidate.order_id,
                         _decimal_text(candidate.risk_amount), _decimal_text(candidate.margin_amount),
                         _decimal_text(candidate.risk_amount), _decimal_text(candidate.margin_amount), now, now),
                    )
                    used_risk += candidate.risk_amount
                    used_margin += candidate.margin_amount
                    decisions.append(ReservationDecision(candidate, True))
        return tuple(decisions)

    def reserve_candidates_with_traces(
        self, candidates: tuple[ReservationCandidate, ...], *, risk_budget: Decimal,
        margin_budget: Decimal, created_at: datetime | None = None,
    ) -> tuple[tuple[ReservationDecision, ...], tuple[CalculationTrace, ...]]:
        """Reserve candidates and retain pre-reservation budgets and decision reasons."""
        decisions = self.reserve_candidates(candidates, risk_budget=risk_budget,
                                            margin_budget=margin_budget, created_at=created_at)
        traces = tuple(calculation_trace(
            "portfolio.reservation", inputs={
                "risk_budget": MeasuredValue(risk_budget, "RUB"),
                "margin_budget": MeasuredValue(margin_budget, "RUB"),
                "candidate_risk": MeasuredValue(decision.candidate.risk_amount, "RUB"),
                "candidate_margin": MeasuredValue(decision.candidate.margin_amount, "RUB"),
                "priority": MeasuredValue(decision.candidate.priority, "priority"),
            }, result=MeasuredValue(decision.accepted, "reservation-accepted"),
            reason=decision.reason or "reserved",
            formula="sorted candidates consume local risk and margin budgets atomically",
            links=TraceLinks(assignment_id=decision.candidate.assignment_id, trade_id=decision.candidate.trade_id,
                             signal_id=decision.candidate.signal_id, command_id=decision.candidate.order_id),
            outcome=TraceOutcome.ACCEPTED if decision.accepted else TraceOutcome.REJECTED,
        ) for decision in decisions)
        with self.transaction() as connection:
            for trace in traces:
                self._trace_repository.record_in_transaction(connection, trace)
        return decisions, traces

    def enqueue(
        self,
        command_id: str,
        trade_id: str,
        payload: Mapping[str, object],
        *,
        created_at: datetime | None = None,
    ) -> bool:
        """Persist a command intent once, before any broker call is attempted."""
        if not command_id or not trade_id:
            raise ValueError("command_id and trade_id are required")
        with self.transaction() as connection:
            return self._enqueue(connection, command_id, trade_id, payload, created_at)

    def record_signal_and_enqueue(
        self,
        assignment_id: str,
        signal_id: str,
        command_id: str,
        trade_id: str,
        payload: Mapping[str, object],
        *,
        processed_at: datetime | None = None,
    ) -> bool:
        """Atomically deduplicate a signal and create its command intent.

        A failed transaction records neither side, so replaying the signal is safe.
        """
        if not assignment_id or not signal_id:
            raise ValueError("assignment_id and signal_id are required")
        now = (processed_at or self._now()).isoformat()
        with self.transaction() as connection:
            inserted = connection.execute(
                "INSERT INTO processed_signals (assignment_id, signal_id, trade_id, processed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (assignment_id, signal_id) DO NOTHING",
                (assignment_id, signal_id, trade_id, now),
            ).rowcount
            if not inserted:
                return False
            self._enqueue(connection, command_id, trade_id, payload, processed_at)
        return True

    def claim_outbox(self, limit: int = 1) -> tuple[OutboxCommand, ...]:
        """Claim pending intents for delivery without making a network guarantee.

        A process crash after this commit can redeliver the same ``command_id`` on
        restart. Brokers must therefore treat command IDs idempotently.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT command_id, trade_id, payload_json, created_at FROM outbox "
                "WHERE status = 'PENDING' ORDER BY created_at, command_id LIMIT ?",
                (limit,),
            ).fetchall()
            command_ids = [row[0] for row in rows]
            if command_ids:
                placeholders = ", ".join("?" for _ in command_ids)
                connection.execute(
                    f"UPDATE outbox SET status = 'CLAIMED' WHERE command_id IN ({placeholders}) "
                    "AND status = 'PENDING'",
                    command_ids,
                )
            return tuple(
                OutboxCommand(
                    command_id=row[0],
                    trade_id=row[1],
                    payload=json.loads(row[2]),
                    created_at=datetime.fromisoformat(row[3]),
                )
                for row in rows
            )

    def mark_outbox_sent(self, command_id: str, *, sent_at: datetime | None = None) -> bool:
        """Record a successful submission of a claimed command, not its execution."""
        with self.transaction() as connection:
            return bool(connection.execute(
                "UPDATE outbox SET status = 'SENT', sent_at = ? "
                "WHERE command_id = ? AND status = 'CLAIMED'",
                ((sent_at or self._now()).isoformat(), command_id),
            ).rowcount)

    def requeue_claimed_outbox(self) -> int:
        """Make unconfirmed delivery attempts retryable after a process restart."""
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE outbox SET status = 'PENDING' WHERE status = 'CLAIMED'"
            ).rowcount

    def release_terminal_reservations(self) -> int:
        """Free risk and margin still reserved by trades that already finished.

        Reservations of a filled entry are released by its own execution event,
        but an unfilled entry is cancelled through a separate ``CANCEL`` command
        and its reservation used to survive the trade.  Repairing the leftovers
        on startup keeps a stale reservation from blocking every later entry.
        """
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE reservations SET risk_amount='0', margin_amount='0', status='RELEASED', "
                "updated_at=datetime('now') WHERE status = 'ACTIVE' AND trade_id IN "
                "(SELECT trade_id FROM trades WHERE phase IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR'))"
            ).rowcount

    def load_trades(self, *, include_terminal: bool = False) -> tuple[RecoveredTrade, ...]:
        """Load durable trade-management state without consulting live configuration.

        CSV exports are intentionally not read: SQLite is the only recovery source.
        Corrupt persisted JSON is treated as a failed recovery rather than silently
        creating a different plan or profile.
        """
        query = "SELECT * FROM trades"
        if not include_terminal:
            query += " WHERE phase NOT IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR')"
        query += " ORDER BY created_at, trade_id"
        cursor = self.connection.execute(query)
        columns = tuple(column[0] for column in cursor.description)
        return tuple(
            self._load_trade(dict(zip(columns, row, strict=True)))
            for row in cursor.fetchall()
        )

    def load_trade(self, trade_id: str, *, include_terminal: bool = False) -> RecoveredTrade | None:
        """Load one trade without re-registering it anywhere.

        ``load_trades`` is the recovery path and re-publishes plans to the
        broker; this one is for reading state in place, where the broker's copy
        is already the newer of the two.
        """
        query = "SELECT * FROM trades WHERE trade_id = ?"
        if not include_terminal:
            query += " AND phase NOT IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR')"
        cursor = self.connection.execute(query, (trade_id,))
        columns = tuple(column[0] for column in cursor.description)
        row = cursor.fetchone()
        return None if row is None else self._load_trade(dict(zip(columns, row, strict=True)))

    def _load_trade(self, trade: dict[str, object]) -> RecoveredTrade:
        try:
            plan_data = json.loads(trade["plan_json"])
            profile_data = json.loads(trade["profile_json"])
            profile_state = json.loads(trade["profile_state_json"])
            targets = self.connection.execute(
                "SELECT target_id, price, status, filled_quantity FROM targets WHERE trade_id = ? ORDER BY target_index",
                (trade["trade_id"],),
            ).fetchall()
            protection = self.connection.execute(
                "SELECT confirmed_stop, pending_stop FROM protection WHERE trade_id = ?",
                (trade["trade_id"],),
            ).fetchone()
            position = self.connection.execute(
                "SELECT quantity, average_price, realized_pnl, fees FROM positions WHERE trade_id = ?",
                (trade["trade_id"],),
            ).fetchone()

            target_shares = {
                target["target_id"]: target["share"]
                for target in plan_data["targets"]
            }
            target_steps = {
                target["target_id"]: target.get("initial_step")
                for target in plan_data["targets"]
            }
            target_data = {t["target_id"]: t for t in plan_data["targets"]}
            version = plan_data.get("algorithm_version", LEGACY_ALGORITHM)
            costs = plan_data.get("cost_snapshot")
            plan = TradePlan(
                trade_id=trade["trade_id"],
                assignment_id=trade["assignment_id"],
                instrument_id=trade["instrument_id"],
                side=trade["side"],
                signal_id=trade["signal_id"],
                reference_entry=Decimal(plan_data["reference_entry"]),
                stop_price=Decimal(plan_data["stop_price"]),
                targets=tuple(
                    TargetPlan(
                        target_id, Decimal(target_data[target_id].get("price", price) if version == CURRENT_ALGORITHM else price), Decimal(target_shares[target_id]),
                        Decimal(target_steps[target_id] or "0"),
                        target_data[target_id].get("price_basis", "r"),
                    )
                    for target_id, price, _, _ in targets
                ),
                profile=ProfileSnapshot(
                    name=profile_data["name"],
                    version=profile_data["version"],
                    parameters=profile_data["parameters"],
                ),
                created_at=datetime.fromisoformat(trade["created_at"]),
                timeframe=str(plan_data.get("timeframe", "")),
                stop_basis=_stop_basis_from_payload(plan_data.get("stop_basis")),
                economics=_economics_from_payload(plan_data.get("economics")),
                algorithm_version=version,
                entry_order_type=plan_data.get("entry_order_type", "market"),
                requested_quantity=plan_data.get("requested_quantity"),
                price_step=None if plan_data.get("price_step") is None else Decimal(plan_data["price_step"]),
                cost_snapshot=None if costs is None else CostSnapshot(Decimal(costs["commission"]), Decimal(costs["slippage"])),
                admission_snapshot=plan_data.get("admission_snapshot", {}),
            )
            quantity, average_price, gross, fees = position if position is not None else (0, None, "0", "0")
            confirmed_stop, pending_stop = protection if protection is not None else (None, None)
            measurement = self.connection.execute(
                "SELECT initial_stop_distance,max_quantity FROM trade_measurements WHERE trade_id=?", (trade["trade_id"],),
            ).fetchone()
            unknown_fees = self.connection.execute(
                "SELECT 1 FROM fills WHERE trade_id=? AND fee_source='unknown' LIMIT 1", (trade["trade_id"],),
            ).fetchone()
            state = TradeState(
                trade_id=trade["trade_id"],
                phase=TradePhase(trade["phase"]),
                state_revision=trade["state_revision"],
                quantity=quantity,
                average_price=Decimal(average_price) if average_price is not None else None,
                completed_target_ids=frozenset(
                    target_id for target_id, _, status, _ in targets if status == "FILLED"
                ),
                add_count=max(int(profile_state.get("add_count", 0)), self.connection.execute(
                    "SELECT COUNT(DISTINCT f.order_id) FROM fills f JOIN orders o ON o.order_id=f.order_id "
                    "WHERE f.trade_id=? AND o.action_type='ADD'", (trade["trade_id"],)).fetchone()[0]),
                trailing_extreme=(
                    Decimal(profile_state["trailing_extreme"])
                    if profile_state.get("trailing_extreme") is not None else None
                ),
                confirmed_stop=Decimal(confirmed_stop) if confirmed_stop is not None else None,
                pending_stop=Decimal(pending_stop) if pending_stop is not None else None,
                target_prices={target_id: Decimal(price) for target_id, price, _, _ in targets},
                initial_stop_distance=(
                    Decimal(measurement[0]) if measurement and measurement[0] is not None
                    else Decimal(str(profile_state["initial_stop_distance"])) if profile_state.get("initial_stop_distance") is not None
                    else None
                ),
                max_quantity=measurement[1] if measurement else int(profile_state.get("max_quantity", quantity)),
                realized_pnl=Decimal(gross), fees=Decimal(fees), fees_known=unknown_fees is None,
                trailing_active=bool(profile_state.get("trailing_active", False)),
                adds_disabled=bool(profile_state.get("adds_disabled", False)),
            )
            entry_ack_at = None
            entry_quantity = self.connection.execute(
                "SELECT COALESCE(SUM(f.quantity), 0) FROM fills f "
                "JOIN orders o ON o.order_id = f.order_id "
                "WHERE f.trade_id = ? AND o.action_type IN ('OPEN', 'ADD')",
                (trade["trade_id"],),
            ).fetchone()[0]
            entry_order = self.connection.execute(
                "SELECT status, updated_at FROM orders WHERE trade_id = ? AND action_type = 'OPEN' "
                "ORDER BY created_at LIMIT 1",
                (trade["trade_id"],),
            ).fetchone()
            if entry_order is not None and entry_order[0] == "ACK" and entry_order[1]:
                try:
                    ack = datetime.fromisoformat(entry_order[1])
                except ValueError:
                    ack = None
                if ack is not None and ack.tzinfo is not None:
                    entry_ack_at = ack
            pending = []
            for payload, remaining, ack_at, created_at in self.connection.execute(
                "SELECT b.payload_json, o.quantity-o.filled_quantity, "
                "(SELECT e.occurred_at FROM events e WHERE e.command_id=o.command_id "
                "AND e.event_type='ACK' ORDER BY e.event_seq LIMIT 1), b.created_at "
                "FROM orders o JOIN outbox b ON b.command_id=o.command_id "
                "WHERE o.trade_id=? AND o.action_type IN ('OPEN','ADD') "
                "AND o.status IN ('ACK','PARTIAL') AND b.status='SENT' "
                "AND o.quantity>o.filled_quantity ORDER BY o.created_at,o.order_id",
                (trade["trade_id"],),
            ):
                action = replace(action_from_payload(json.loads(payload)), quantity=remaining)
                pending.append(PendingIncrease(action, datetime.fromisoformat(ack_at or created_at)))
            pending_stops = []
            for payload, ack_at, created_at in self.connection.execute(
                "SELECT b.payload_json, (SELECT e.occurred_at FROM events e WHERE e.command_id=o.command_id "
                "AND e.event_type='ACK' ORDER BY e.event_seq LIMIT 1), b.created_at "
                "FROM orders o JOIN outbox b ON b.command_id=o.command_id "
                "JOIN protection s ON s.pending_command_id=o.command_id "
                "WHERE o.trade_id=? AND o.action_type='MOVESTOP' AND o.status='ACK' AND b.status='SENT'",
                (trade["trade_id"],),
            ):
                pending_stops.append(PendingStop(action_from_payload(json.loads(payload)), datetime.fromisoformat(ack_at or created_at)))
            last_increase = self.connection.execute(
                "SELECT f.executed_at,f.price FROM fills f JOIN orders o ON o.order_id=f.order_id "
                "WHERE f.trade_id=? AND o.action_type IN ('OPEN','ADD') ORDER BY f.executed_at DESC,f.rowid DESC LIMIT 1",
                (trade["trade_id"],),
            ).fetchone()
            pending_exits = []
            for payload, ack_at, created_at in self.connection.execute(
                "SELECT b.payload_json, (SELECT e.occurred_at FROM events e WHERE e.command_id=o.command_id "
                "AND e.event_type='ACK' ORDER BY e.event_seq LIMIT 1), b.created_at "
                "FROM orders o JOIN outbox b ON b.command_id=o.command_id "
                "WHERE o.trade_id=? AND o.action_type IN ('CLOSE','REDUCE') AND o.status='ACK' AND b.status='SENT'",
                (trade["trade_id"],),
            ):
                pending_exits.append(PendingExit(action_from_payload(json.loads(payload)), datetime.fromisoformat(ack_at or created_at)))
            last_bar = self.connection.execute("SELECT bar_id FROM trade_market_observations WHERE trade_id=? AND timeframe='1m' ORDER BY bar_id DESC LIMIT 1",
                                               (trade["trade_id"],)).fetchone()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot recover trade {trade['trade_id']!r} from SQLite") from error
        return RecoveredTrade(
            plan=plan,
            state=state,
            entry_ack_at=entry_ack_at,
            entry_quantity=entry_quantity,
            target_filled={target_id: filled for target_id, _, _, filled in targets},
            pending_increases=tuple(pending),
            pending_stops=tuple(pending_stops),
            pending_exits=tuple(pending_exits),
            last_increase_at=datetime.fromisoformat(last_increase[0]) if last_increase else None,
            last_increase_price=Decimal(last_increase[1]) if last_increase else None,
            last_observed_bar=datetime.fromisoformat(last_bar[0]) if last_bar else None,
        )

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        command_id: str,
        trade_id: str,
        payload: Mapping[str, object],
        created_at: datetime | None,
    ) -> bool:
        now = (created_at or self._now()).isoformat()
        return bool(connection.execute(
            "INSERT INTO outbox (command_id, trade_id, payload_json, status, created_at) "
            "VALUES (?, ?, ?, 'PENDING', ?) ON CONFLICT (command_id) DO NOTHING",
            (command_id, trade_id, json.dumps(payload, sort_keys=True), now),
        ).rowcount)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _stop_basis_from_payload(value: object) -> StopBasis:
    """Основание выбора стопа из журнала; неизвестное или отсутствующее — структурное."""
    try:
        return StopBasis(str(value))
    except ValueError:
        return StopBasis.STRUCTURAL


def _economics_from_payload(value: object) -> PlanEconomics | None:
    """Денежное представление плана из журнала; у старых сделок его не было."""
    if not isinstance(value, dict):
        return None
    ratio = value.get("payoff_ratio")
    return PlanEconomics(
        quantity=int(value["quantity"]),
        risk_amount=Decimal(value["risk_amount"]),
        reward_amount=None if value.get("reward_amount") is None else Decimal(value["reward_amount"]),
        costs_amount=Decimal(value["costs_amount"]),
        payoff_ratio=None if ratio is None else Decimal(str(ratio)),
        fixed_reward_amount=None if value.get("fixed_reward_amount") is None else Decimal(str(value["fixed_reward_amount"])),
        fixed_quantity=int(value.get("fixed_quantity", 0)),
        target_quantities=value.get("target_quantities", {}),
        slippage_amount=Decimal(str(value.get("slippage_amount", "0"))),
    )
