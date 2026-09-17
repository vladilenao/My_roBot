"""Atomic CSV projections of the SQLite trade journal."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import logging
import logging.handlers
import os
from pathlib import Path
import tempfile


log = logging.getLogger(__name__)


JOURNAL_COLUMNS = (
    "export_revision", "event_seq", "event_id", "occurred_at", "event_type",
    "trade_id", "assignment_id", "instrument_id", "command_id", "order_id", "payload",
)
POSITIONS_COLUMNS = (
    "export_revision", "position_id", "assignment_id", "instrument_id", "side", "phase",
    "quantity", "average_price", "realized_pnl", "fees", "net_realized_pnl", "updated_at",
)


class CsvExporter:
    """Exports both user-facing projections from one read-only SQLite snapshot."""

    def __init__(self, connection, journal_path: Path, positions_path: Path) -> None:
        self._connection = connection
        self._journal_path = journal_path
        self._positions_path = positions_path

    def export(self) -> bool:
        """Replace both projections, retaining prior complete files on any failure."""
        revision, journal_rows, position_rows = self._snapshot()
        files: list[tuple[Path, Path]] = []
        try:
            self._backup_legacy_files()
            files = [
                (self._write_temp(self._journal_path, JOURNAL_COLUMNS, journal_rows), self._journal_path),
                (self._write_temp(self._positions_path, POSITIONS_COLUMNS, position_rows), self._positions_path),
            ]
            errors: list[OSError] = []
            for temporary, destination in files:
                try:
                    os.replace(temporary, destination)
                except OSError as error:
                    errors.append(error)
            if errors:
                raise errors[0]
        except OSError as error:
            for temporary, _ in files:
                temporary.unlink(missing_ok=True)
            self._record_failure(revision, error)
            log.error("CSV export revision %s failed: %s", revision, error)
            return False
        self._record_success(revision)
        return True

    def _backup_legacy_files(self) -> None:
        """Preserve pre-SQLite CSVs once, before their first replacement."""
        for path, columns in (
            (self._journal_path, JOURNAL_COLUMNS),
            (self._positions_path, POSITIONS_COLUMNS),
        ):
            if path.exists() and not self._is_current_projection(path, columns):
                self._backup_legacy_file(path)

    @staticmethod
    def _is_current_projection(path: Path, columns: tuple[str, ...]) -> bool:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                return tuple(csv.DictReader(handle).fieldnames or ()) == columns
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def _backup_legacy_file(path: Path) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = 0
        while True:
            disambiguator = "" if suffix == 0 else f".{suffix}"
            backup = path.with_name(f"{path.name}.legacy.{timestamp}{disambiguator}")
            try:
                with path.open("rb") as source, backup.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
                return
            except FileExistsError:
                suffix += 1

    def _snapshot(self) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
        self._connection.execute("BEGIN")
        try:
            revision = self._connection.execute(
                "SELECT required_revision FROM export_state WHERE export_id = 1"
            ).fetchone()[0]
            journal_rows = self._rows(
                """
                SELECT event_seq, event_id, occurred_at, event_type, events.trade_id,
                       trades.assignment_id, trades.instrument_id, command_id, order_id, payload_json
                FROM events LEFT JOIN trades ON trades.trade_id = events.trade_id
                ORDER BY event_seq
                """,
                JOURNAL_COLUMNS,
                revision,
            )
            position_rows = self._rows(
                """
                SELECT positions.trade_id, trades.assignment_id, trades.instrument_id, positions.side,
                       trades.phase, positions.quantity, positions.average_price, positions.realized_pnl,
                       positions.fees, positions.net_realized_pnl, positions.updated_at
                FROM positions JOIN trades ON trades.trade_id = positions.trade_id
                ORDER BY positions.trade_id
                """,
                POSITIONS_COLUMNS,
                revision,
            )
        finally:
            self._connection.rollback()
        return revision, journal_rows, position_rows

    def _rows(self, query: str, columns: tuple[str, ...], revision: int) -> list[dict[str, object]]:
        cursor = self._connection.execute(query)
        names = tuple(column[0] for column in cursor.description)
        result = []
        for row in cursor.fetchall():
            values = dict(zip(names, row, strict=True))
            values["export_revision"] = revision
            result.append({column: values.get(column, "") for column in columns})
        return result

    @staticmethod
    def _write_temp(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _record_success(self, revision: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET exported_revision = ?, failed_revision = NULL, "
                "last_error = NULL, updated_at = datetime('now') WHERE export_id = 1 "
                "AND required_revision <= ?",
                (revision, revision),
            )

    def _record_failure(self, revision: int, error: OSError) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET failed_revision = ?, last_error = ?, updated_at = datetime('now') "
                "WHERE export_id = 1",
                (revision, str(error)),
            )


class AuditExporter:
    """Exports each durable calculation trace once per successful file write."""

    def __init__(self, connection, path: Path, *, max_bytes: int, backup_count: int) -> None:
        self._connection = connection
        self._path = path
        self._handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def export(self) -> bool:
        """Retry unexported traces without changing their SQLite records."""
        revision = self._connection.execute(
            "SELECT required_revision FROM export_state WHERE export_id = 1"
        ).fetchone()[0]
        rows = self._connection.execute(
            """
            SELECT calculation_id, source_data_id, trade_id, command_id, event_id,
                   assignment_id, signal_id, service_uid, correlation_id, algorithm,
                   algorithm_version, input_json, steps_json, rounding_json, output_json,
                   outcome, reason, created_at
            FROM calculations
            WHERE calculation_id NOT IN (SELECT calculation_id FROM audit_exports)
            ORDER BY created_at, calculation_id
            """
        ).fetchall()
        columns = (
            "calculation_id", "source_data_id", "trade_id", "command_id", "event_id",
            "assignment_id", "signal_id", "service_uid", "correlation_id", "algorithm",
            "algorithm_version", "inputs", "steps", "rounding", "result", "outcome",
            "reason", "created_at",
        )
        try:
            for row in rows:
                record = dict(zip(columns, row, strict=True))
                for field in ("inputs", "steps", "rounding", "result"):
                    record[field] = json.loads(record[field])
                self._write(record)
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO audit_exports (calculation_id, exported_at) VALUES (?, datetime('now'))",
                        (record["calculation_id"],),
                    )
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            self._record_failure(revision, error)
            log.error("trade audit export revision %s failed: %s", revision, error)
            return False
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET audit_exported_revision = ?, audit_failed_revision = NULL, "
                "audit_last_error = NULL, updated_at = datetime('now') WHERE export_id = 1",
                (revision,),
            )
        return True

    def close(self) -> None:
        self._handler.close()

    def _write(self, record: dict[str, object]) -> None:
        # Write directly so I/O errors are visible to the durable retry loop.
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        self._handler.acquire()
        try:
            if self._handler.shouldRollover(logging.makeLogRecord({"msg": line, "levelno": logging.INFO})):
                self._handler.doRollover()
            if self._handler.stream is None:
                self._handler.stream = self._handler._open()
            self._handler.stream.write(line)
            self._handler.flush()
        finally:
            self._handler.release()

    def _record_failure(self, revision: int, error: Exception) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET audit_failed_revision = ?, audit_last_error = ?, "
                "updated_at = datetime('now') WHERE export_id = 1",
                (revision, str(error)),
            )
