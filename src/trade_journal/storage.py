"""SQLite connection and transaction helpers for the trade journal."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from decimal import Decimal
from typing import Iterator, Mapping

from src.trade_journal.schema import initialize_schema
from src.trade_journal.export import AuditExporter, CsvExporter
from src.trade_management.models import (
    ProfileSnapshot,
    TargetPlan,
    TradePhase,
    TradePlan,
    TradeState,
)
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace


BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class OutboxCommand:
    """A durable command intent ready to be submitted to a broker."""

    command_id: str
    trade_id: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class RecoveredTrade:
    """The complete persisted management state for one trade."""

    plan: TradePlan
    state: TradeState


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


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a database only after its schema is known to this application."""
    connection = sqlite3.connect(database)
    try:
        configure_connection(connection)
        initialize_schema(connection)
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
        self.connection = connect(database)
        self._exporter = (
            CsvExporter(self.connection, Path(journal_path), Path(positions_path))
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
        self.export()

    def close(self) -> None:
        if self._audit_exporter is not None:
            self._audit_exporter.close()
        self.connection.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

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
        """Hand ticker -> short contract name mapping to the CSV projections."""
        if self._exporter is None:
            return
        self._exporter.set_names(names)
        self.export()

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
        now = (created_at or datetime.now(timezone.utc)).isoformat()
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
        now = (processed_at or datetime.now(timezone.utc)).isoformat()
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
                ((sent_at or datetime.now(timezone.utc)).isoformat(), command_id),
            ).rowcount)

    def requeue_claimed_outbox(self) -> int:
        """Make unconfirmed delivery attempts retryable after a process restart."""
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE outbox SET status = 'PENDING' WHERE status = 'CLAIMED'"
            ).rowcount

    def load_trades(self, *, include_terminal: bool = False) -> tuple[RecoveredTrade, ...]:
        """Load durable trade-management state without consulting live configuration.

        CSV exports are intentionally not read: SQLite is the only recovery source.
        Corrupt persisted JSON is treated as a failed recovery rather than silently
        creating a different plan or profile.
        """
        query = "SELECT * FROM trades"
        if not include_terminal:
            query += " WHERE phase NOT IN ('CLOSED', 'CANCELLED')"
        query += " ORDER BY created_at, trade_id"
        cursor = self.connection.execute(query)
        columns = tuple(column[0] for column in cursor.description)
        return tuple(
            self._load_trade(dict(zip(columns, row, strict=True)))
            for row in cursor.fetchall()
        )

    def _load_trade(self, trade: dict[str, object]) -> RecoveredTrade:
        try:
            plan_data = json.loads(trade["plan_json"])
            profile_data = json.loads(trade["profile_json"])
            profile_state = json.loads(trade["profile_state_json"])
            targets = self.connection.execute(
                "SELECT target_id, price, status FROM targets WHERE trade_id = ? ORDER BY target_index",
                (trade["trade_id"],),
            ).fetchall()
            protection = self.connection.execute(
                "SELECT confirmed_stop, pending_stop FROM protection WHERE trade_id = ?",
                (trade["trade_id"],),
            ).fetchone()
            position = self.connection.execute(
                "SELECT quantity, average_price FROM positions WHERE trade_id = ?",
                (trade["trade_id"],),
            ).fetchone()

            target_shares = {
                target["target_id"]: target["share"]
                for target in plan_data["targets"]
            }
            plan = TradePlan(
                trade_id=trade["trade_id"],
                assignment_id=trade["assignment_id"],
                instrument_id=trade["instrument_id"],
                side=trade["side"],
                signal_id=trade["signal_id"],
                reference_entry=Decimal(plan_data["reference_entry"]),
                stop_price=Decimal(plan_data["stop_price"]),
                targets=tuple(
                    TargetPlan(target_id, Decimal(price), Decimal(target_shares[target_id]))
                    for target_id, price, _ in targets
                ),
                profile=ProfileSnapshot(
                    name=profile_data["name"],
                    version=profile_data["version"],
                    parameters=profile_data["parameters"],
                ),
                created_at=datetime.fromisoformat(trade["created_at"]),
            )
            quantity, average_price = position if position is not None else (0, None)
            confirmed_stop, pending_stop = protection if protection is not None else (None, None)
            state = TradeState(
                trade_id=trade["trade_id"],
                phase=TradePhase(trade["phase"]),
                state_revision=trade["state_revision"],
                quantity=quantity,
                average_price=Decimal(average_price) if average_price is not None else None,
                completed_target_ids=frozenset(
                    target_id for target_id, _, status in targets if status == "FILLED"
                ),
                add_count=int(profile_state.get("add_count", 0)),
                trailing_extreme=(
                    Decimal(profile_state["trailing_extreme"])
                    if profile_state.get("trailing_extreme") is not None else None
                ),
                confirmed_stop=Decimal(confirmed_stop) if confirmed_stop is not None else None,
                pending_stop=Decimal(pending_stop) if pending_stop is not None else None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot recover trade {trade['trade_id']!r} from SQLite") from error
        return RecoveredTrade(plan=plan, state=state)

    @staticmethod
    def _enqueue(
        connection: sqlite3.Connection,
        command_id: str,
        trade_id: str,
        payload: Mapping[str, object],
        created_at: datetime | None,
    ) -> bool:
        now = (created_at or datetime.now(timezone.utc)).isoformat()
        return bool(connection.execute(
            "INSERT INTO outbox (command_id, trade_id, payload_json, status, created_at) "
            "VALUES (?, ?, ?, 'PENDING', ?) ON CONFLICT (command_id) DO NOTHING",
            (command_id, trade_id, json.dumps(payload, sort_keys=True), now),
        ).rowcount)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
