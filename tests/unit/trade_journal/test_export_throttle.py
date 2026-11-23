"""Отложенный экспорт проекций журнала и поиск без перебора таблиц (fix-history-export-throughput)."""

import csv
from decimal import Decimal
import json
import sqlite3

import pytest

from src.trade_journal import schema
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

NOW = "2026-01-01T00:00:00+00:00"


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _open_trade(storage, trade_id):
    with storage.transaction() as connection:
        connection.execute(
            "INSERT INTO trades VALUES (?, ?, 'NGV6', ?, 'BUY', '{}', '{}', 'OPEN', 1, '{}', ?, ?, NULL, NULL)",
            (trade_id, f"assignment-{trade_id}", f"signal-{trade_id}", NOW, NOW),
        )
        connection.execute("INSERT INTO positions VALUES (?, 'BUY', 2, '100', '0', '0', '0', ?)", (trade_id, NOW))
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, '{}', 'SENT', ?, NULL)", (f"cmd-{trade_id}", trade_id, NOW),
        )
        connection.execute(
            "INSERT INTO orders VALUES (?, ?, ?, 'OPEN', 'FILLED', 2, 2, '100', ?, ?)",
            (f"order-{trade_id}", trade_id, f"cmd-{trade_id}", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO fills (fill_id,order_id,trade_id,command_id,execution_id,quantity,price,fee,executed_at) "
            "VALUES (?, ?, ?, ?, ?, 2, '100', '0', ?)",
            (f"fill-{trade_id}", f"order-{trade_id}", trade_id, f"cmd-{trade_id}", f"exec-{trade_id}", NOW),
        )
        connection.execute(
            "INSERT INTO events (event_id,trade_id,order_id,command_id,event_type,payload_json,occurred_at) "
            "VALUES (?, ?, ?, ?, 'FILL', '{}', ?)",
            (f"exec-{trade_id}", trade_id, f"order-{trade_id}", f"cmd-{trade_id}", NOW),
        )


def _summary_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _revisions(storage):
    return storage.connection.execute(
        "SELECT required_revision, exported_revision FROM export_state"
    ).fetchone()


def _storage(tmp_path, monotonic, interval=30.0):
    return Storage(
        tmp_path / "trades.sqlite3",
        journal_path=tmp_path / "trade_event.csv",
        positions_path=tmp_path / "trade_summary.csv",
        export_interval=interval,
        monotonic=monotonic,
    )


def test_transactions_inside_interval_defer_csv_until_interval_passes(tmp_path):
    monotonic = FakeMonotonic()
    summary = tmp_path / "trade_summary.csv"
    storage = _storage(tmp_path, monotonic)
    try:
        assert _summary_rows(summary) == []  # стартовый экспорт как прежде

        monotonic.value += 10
        _open_trade(storage, "trade-1")
        assert _summary_rows(summary) == []
        required, exported = _revisions(storage)
        assert exported < required

        monotonic.value += 25  # 35 с с последнего экспорта
        _open_trade(storage, "trade-2")
        assert len(_summary_rows(summary)) == 2
        assert _revisions(storage)[1] == _revisions(storage)[0]
    finally:
        storage.close()


def test_flush_writes_deferred_projection_and_is_noop_when_nothing_pending(tmp_path):
    monotonic = FakeMonotonic()
    summary = tmp_path / "trade_summary.csv"
    storage = _storage(tmp_path, monotonic)
    try:
        _open_trade(storage, "trade-1")
        assert _summary_rows(summary) == []

        assert storage.flush_exports()
        assert len(_summary_rows(summary)) == 1
        required, exported = _revisions(storage)
        assert exported == required

        summary.unlink()
        assert storage.flush_exports()
        assert not summary.exists()  # без отложенных изменений файлы не трогаются
    finally:
        storage.close()


def test_close_flushes_deferred_projection(tmp_path):
    storage = _storage(tmp_path, FakeMonotonic())
    _open_trade(storage, "trade-1")
    assert _summary_rows(tmp_path / "trade_summary.csv") == []

    storage.close()

    assert len(_summary_rows(tmp_path / "trade_summary.csv")) == 1


def test_failed_export_stays_pending_for_flush(tmp_path, monkeypatch):
    monotonic = FakeMonotonic()
    storage = _storage(tmp_path, monotonic)
    try:
        _open_trade(storage, "trade-1")
        monkeypatch.setattr(storage._exporter, "export", lambda: False)
        monotonic.value += 60
        _open_trade(storage, "trade-2")
        monkeypatch.undo()

        assert storage.flush_exports()
        assert len(_summary_rows(tmp_path / "trade_summary.csv")) == 2
    finally:
        storage.close()


def test_quiet_period_projection_is_written_by_tick_check_after_interval(tmp_path):
    monotonic = FakeMonotonic()
    summary = tmp_path / "trade_summary.csv"
    storage = _storage(tmp_path, monotonic)
    try:
        monotonic.value += 5
        _open_trade(storage, "trade-1")  # серия транзакций, затем затишье
        assert storage.export_if_due()
        assert _summary_rows(summary) == []  # интервал ещё не прошёл

        monotonic.value += 30
        assert storage.export_if_due()
        assert len(_summary_rows(summary)) == 1
        required, exported = _revisions(storage)
        assert exported == required

        summary.unlink()
        monotonic.value += 60
        assert storage.export_if_due()
        assert not summary.exists()  # без отложенных изменений файлы не трогаются
    finally:
        storage.close()


def test_tick_check_is_noop_without_interval(tmp_path, monkeypatch):
    storage = _storage(tmp_path, FakeMonotonic(), interval=None)
    try:
        monkeypatch.setattr(storage._exporter, "export", lambda: False)
        _open_trade(storage, "trade-1")  # неудачный экспорт боевого режима
        monkeypatch.undo()
        calls = []
        monkeypatch.setattr(storage, "export", lambda: calls.append(1) or True)
        assert storage.export_if_due()
        assert calls == []  # боевой режим повторяет экспорт после транзакции, не на тике
    finally:
        monkeypatch.undo()
        storage.close()


def test_without_interval_every_transaction_exports(tmp_path):
    storage = _storage(tmp_path, FakeMonotonic(), interval=None)
    try:
        _open_trade(storage, "trade-1")
        assert len(_summary_rows(tmp_path / "trade_summary.csv")) == 1
        required, exported = _revisions(storage)
        assert exported == required
    finally:
        storage.close()


def test_negative_interval_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="export interval"):
        _storage(tmp_path, FakeMonotonic(), interval=-1)


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


def test_audit_export_does_not_reread_traces_below_watermark(tmp_path):
    audit = tmp_path / "trade_decision_trace.log"
    with Storage(tmp_path / "trades.sqlite3", audit_path=audit) as storage:
        repository = CalculationTraceRepository(storage)
        assert repository.record(_trace("calculation-1"))
        # Метка отсекает выгруженное: потерянная отметка не вызывает повторной выгрузки.
        storage.connection.execute("DELETE FROM audit_exports")
        storage.connection.commit()
        assert repository.record(_trace("calculation-2"))

    exported = [json.loads(line)["calculation_id"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert exported == ["calculation-1", "calculation-2"]


def test_new_audit_exporter_retries_traces_left_unexported(tmp_path):
    database = tmp_path / "trades.sqlite3"
    audit = tmp_path / "trade_decision_trace.log"
    with Storage(database) as storage:
        assert CalculationTraceRepository(storage).record(_trace("calculation-1"))

    with Storage(database, audit_path=audit):
        pass

    exported = [json.loads(line)["calculation_id"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert exported == ["calculation-1"]


LOOKUP_INDEXES = {
    "fills_by_trade", "events_by_trade", "orders_by_trade", "outbox_by_trade", "calculations_by_trade",
    "trades_by_instrument", "observations_by_instrument",
}


def _indexes(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_fresh_database_has_lookup_indexes_and_same_version(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        assert LOOKUP_INDEXES <= _indexes(storage.connection)
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION


def test_existing_current_database_gains_lookup_indexes_on_open(tmp_path):
    database = tmp_path / "trades.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(schema.SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version={schema.SCHEMA_VERSION}")
    connection.execute("INSERT INTO export_state(export_id,updated_at) VALUES(1,'2026-01-01')")
    connection.commit()
    assert not LOOKUP_INDEXES & _indexes(connection)
    connection.close()

    with Storage(database) as storage:
        assert LOOKUP_INDEXES <= _indexes(storage.connection)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 FROM fills WHERE trade_id='t'",
        "SELECT 1 FROM events WHERE trade_id='t'",
        "SELECT 1 FROM orders WHERE trade_id='t' AND action_type='OPEN'",
        "SELECT 1 FROM outbox WHERE trade_id='t' AND status='PENDING'",
        "SELECT 1 FROM calculations WHERE trade_id='t' AND algorithm='a'",
        "SELECT trade_id FROM trades WHERE instrument_id='i'",
        "SELECT bar_id FROM trade_market_observations WHERE instrument_id='i' AND timeframe='1m'",
    ],
)
def test_trade_lookups_do_not_scan_whole_tables(tmp_path, query):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        plan = " ".join(row[3] for row in storage.connection.execute("EXPLAIN QUERY PLAN " + query))
    assert "SCAN" not in plan, plan


def _history_storage(tmp_path, monotonic):
    """Журнал исторического прогона: аудит раз в интервал, CSV — только в конце."""
    return Storage(
        tmp_path / "trades.sqlite3",
        journal_path=tmp_path / "trade_event.csv",
        positions_path=tmp_path / "trade_summary.csv",
        audit_path=tmp_path / "trade_decision_trace.log",
        export_interval=30.0,
        csv_on_flush_only=True,
        monotonic=monotonic,
    )


def _audit_ids(path):
    if not path.exists():
        return []
    return [json.loads(line)["calculation_id"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_history_csv_is_not_written_during_run_while_audit_keeps_its_interval(tmp_path):
    monotonic = FakeMonotonic()
    storage = _history_storage(tmp_path, monotonic)
    try:
        assert not (tmp_path / "trade_summary.csv").exists()  # и стартового экспорта нет
        _open_trade(storage, "trade-1")
        assert CalculationTraceRepository(storage).record(_trace("calculation-1"))
        monotonic.value += 60
        assert storage.export_if_due()
        _open_trade(storage, "trade-2")
        monotonic.value += 60
        assert storage.export_if_due()

        assert not (tmp_path / "trade_event.csv").exists()
        assert not (tmp_path / "trade_summary.csv").exists()
        required, exported = _revisions(storage)
        assert exported < required
        assert _audit_ids(tmp_path / "trade_decision_trace.log") == ["calculation-1"]
    finally:
        storage.close()


def test_history_names_and_contract_metadata_do_not_write_csv_mid_run(tmp_path):
    storage = _history_storage(tmp_path, FakeMonotonic())
    try:
        _open_trade(storage, "trade-1")
        storage.set_names({"NGV6": "NG-10.26"})
        storage.set_contract_metadata({})
        assert not (tmp_path / "trade_summary.csv").exists()
    finally:
        storage.close()


def test_history_csv_is_written_once_by_flush_and_matches_journal(tmp_path):
    storage = _history_storage(tmp_path, FakeMonotonic())
    try:
        _open_trade(storage, "trade-1")
        _open_trade(storage, "trade-2")

        assert storage.flush_exports()

        assert len(_summary_rows(tmp_path / "trade_summary.csv")) == 2
        required, exported = _revisions(storage)
        assert exported == required
    finally:
        storage.close()


def test_history_close_writes_csv_even_without_transactions(tmp_path):
    storage = _history_storage(tmp_path, FakeMonotonic())
    storage.close()

    assert _summary_rows(tmp_path / "trade_summary.csv") == []
    assert (tmp_path / "trade_event.csv").exists()


def test_history_failed_final_csv_stays_pending_for_retry(tmp_path, monkeypatch):
    storage = _history_storage(tmp_path, FakeMonotonic())
    try:
        _open_trade(storage, "trade-1")
        monkeypatch.setattr(storage._exporter, "export", lambda: False)
        assert not storage.flush_exports()
        monkeypatch.undo()

        assert storage.flush_exports()
        assert len(_summary_rows(tmp_path / "trade_summary.csv")) == 1
    finally:
        storage.close()
