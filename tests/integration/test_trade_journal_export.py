import csv
from decimal import Decimal
import json
import os

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.audit import (
    CalculationTrace,
    CalculationTraceRepository,
    FormulaStep,
    MeasuredValue,
    Rounding,
    TraceLinks,
    TraceOutcome,
)


def _write_snapshot(storage):
    now = "2026-01-01T00:00:00+00:00"
    with storage.transaction() as connection:
        connection.execute(
            "INSERT INTO trades VALUES ('trade-1', 'assignment-1', 'NGV6', 'signal-1', 'BUY', "
            "'{}', '{}', 'OPEN', 1, '{}', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO positions VALUES ('trade-1', 'BUY', 2, '100', '0', '0', '0', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO events (event_id, trade_id, event_type, payload_json, occurred_at) "
            "VALUES ('event-1', 'trade-1', 'FILL', '{}', ?)",
            (now,),
        )


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _change_snapshot(storage):
    with storage.transaction() as connection:
        connection.execute("UPDATE positions SET quantity = 1 WHERE trade_id = 'trade-1'")


def test_export_uses_short_contract_name_from_storage_names(tmp_path):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "journal.csv"
    positions = tmp_path / "positions.csv"

    with Storage(database, journal_path=journal, positions_path=positions) as storage:
        _write_snapshot(storage)
        storage.set_names({"NGV6": "NG-12.26"})

        journal_rows = _read_csv(journal)
        assert journal_rows
        assert journal_rows[0]["Контракт"] == "NG-12.26"

        position_rows = _read_csv(positions)
        assert position_rows
        assert position_rows[0]["Контракт"] == "NG-12.26"


def test_startup_and_later_export_restore_deleted_csv_from_sqlite(tmp_path):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "journal.csv"
    positions = tmp_path / "positions.csv"

    with Storage(database, journal_path=journal, positions_path=positions) as storage:
        _write_snapshot(storage)
        expected_journal = journal.read_text(encoding="utf-8")
        expected_positions = positions.read_text(encoding="utf-8")
        journal.unlink()
        positions.unlink()
        assert storage.export()

    assert journal.read_text(encoding="utf-8") == expected_journal
    assert positions.read_text(encoding="utf-8") == expected_positions
    with Storage(database, journal_path=journal, positions_path=positions):
        pass
    assert journal.exists()
    assert positions.exists()


def test_transient_csv_failure_keeps_database_and_records_retryable_revision(tmp_path, monkeypatch):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "journal.csv"
    positions = tmp_path / "positions.csv"
    original_replace = os.replace

    with Storage(database, journal_path=journal, positions_path=positions) as storage:
        def fail_journal_replace(source, destination):
            if destination == journal:
                raise PermissionError("locked by spreadsheet")
            return original_replace(source, destination)

        monkeypatch.setattr("src.trade_journal.export.os.replace", fail_journal_replace)
        _write_snapshot(storage)

        required, exported, failed, error = storage.connection.execute(
            "SELECT required_revision, exported_revision, failed_revision, last_error FROM export_state"
        ).fetchone()
        assert storage.connection.execute("SELECT quantity FROM positions").fetchone() == (2,)
        assert failed == required
        assert exported < required
        assert "locked by spreadsheet" in error

        monkeypatch.setattr("src.trade_journal.export.os.replace", original_replace)
        assert storage.export()
        assert storage.connection.execute(
            "SELECT required_revision, exported_revision, failed_revision, last_error FROM export_state"
        ).fetchone() == (required, required, None, None)


def test_rows_expose_lag_until_a_failed_projection_is_retried(tmp_path, monkeypatch):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "journal.csv"
    positions = tmp_path / "positions.csv"
    original_replace = os.replace

    with Storage(database, journal_path=journal, positions_path=positions) as storage:
        _write_snapshot(storage)

        def fail_positions_replace(source, destination):
            if destination == positions:
                raise PermissionError("locked positions")
            return original_replace(source, destination)

        monkeypatch.setattr("src.trade_journal.export.os.replace", fail_positions_replace)
        _change_snapshot(storage)

        stale_rows = _read_csv(positions)
        assert len(stale_rows) == 1
        assert stale_rows[0]["Trade ID"] == "trade-1"

        monkeypatch.setattr("src.trade_journal.export.os.replace", original_replace)
        assert storage.export()

    retried_rows = _read_csv(positions)
    assert len(retried_rows) == 1
    assert retried_rows[0]["Trade ID"] == "trade-1"


def test_first_sqlite_export_preserves_legacy_csvs_only_once(tmp_path):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "trade_event.csv"
    positions = tmp_path / "trade_summary.csv"
    old_journal = tmp_path / "trade_journal.csv"
    old_positions = tmp_path / "trade_journal_positions.csv"
    old_journal.write_text("old journal\nentry\n", encoding="utf-8")
    old_positions.write_text("old positions\nopen\n", encoding="utf-8")

    with Storage(database, journal_path=journal, positions_path=positions) as storage:
        assert storage.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)

    assert journal.exists()
    assert positions.exists()
    assert old_journal.read_text(encoding="utf-8") == "old journal\nentry\n"
    assert old_positions.read_text(encoding="utf-8") == "old positions\nopen\n"

    with Storage(database, journal_path=journal, positions_path=positions) as restarted:
        assert restarted.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)

    assert not list(tmp_path.glob("*.legacy.*"))


def test_storage_rejects_database_path_equal_to_export_path(tmp_path):
    database = tmp_path / "trades.sqlite3"

    with pytest.raises(ValueError, match="database path"):
        Storage(database, journal_path=tmp_path / "." / "trades.sqlite3", positions_path=tmp_path / "positions.csv")
    with pytest.raises(ValueError, match="database path"):
        Storage(database, audit_path=tmp_path / "." / "trades.sqlite3")


def _trace(calculation_id):
    raw = MeasuredValue(Decimal("100.125"), "price")
    rounded = MeasuredValue(Decimal("100"), "price")
    return CalculationTrace(
        calculation_id=calculation_id,
        algorithm="position-sizing",
        algorithm_version="1",
        inputs={"price": raw},
        steps=(FormulaStep("floor(q)", {"q": MeasuredValue(2, "contracts")}, MeasuredValue(2, "contracts")),),
        rounding=(Rounding("floor", raw, rounded),),
        result=MeasuredValue(2, "contracts"),
        outcome=TraceOutcome.ACCEPTED,
        reason="risk-within-limit",
        links=TraceLinks(assignment_id="assignment-1", signal_id="signal-1"),
    )


def test_audit_write_failure_preserves_trace_and_retries_by_calculation_id(tmp_path, monkeypatch):
    database = tmp_path / "trades.sqlite3"
    audit = tmp_path / "trade_decision_trace.log"

    with Storage(database, audit_path=audit) as storage:
        original_write = storage._audit_exporter._write
        monkeypatch.setattr(storage._audit_exporter, "_write", lambda _record: (_ for _ in ()).throw(OSError("disk full")))
        assert CalculationTraceRepository(storage).record(_trace("calculation-1"))

        assert storage.connection.execute("SELECT calculation_id FROM calculations").fetchone() == ("calculation-1",)
        assert storage.connection.execute("SELECT COUNT(*) FROM audit_exports").fetchone() == (0,)
        required, exported, failed, error = storage.connection.execute(
            "SELECT required_revision, audit_exported_revision, audit_failed_revision, audit_last_error FROM export_state"
        ).fetchone()
        assert failed == required
        assert exported < required
        assert "disk full" in error

        monkeypatch.setattr(storage._audit_exporter, "_write", original_write)
        assert storage.export()
        assert storage.connection.execute("SELECT calculation_id FROM audit_exports").fetchone() == ("calculation-1",)
        assert storage.connection.execute(
            "SELECT audit_exported_revision, audit_failed_revision, audit_last_error FROM export_state"
        ).fetchone() == (required, None, None)

    assert [json.loads(line)["calculation_id"] for line in audit.read_text(encoding="utf-8").splitlines()] == ["calculation-1"]
