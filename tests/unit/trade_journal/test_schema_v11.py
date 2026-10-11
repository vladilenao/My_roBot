import sqlite3

import pytest

from src.trade_journal import schema


def legacy_v10():
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema.SCHEMA_V10_SQL)
    connection.execute("PRAGMA user_version=10")
    connection.execute("INSERT INTO export_state(export_id,updated_at) VALUES(1,'2026-01-01')")
    connection.execute("INSERT INTO account VALUES(1,'99888','99880','-100','12','-112','2026-01-01')")
    connection.commit()
    return connection


def test_v11_preserves_v10_financial_state():
    connection = legacy_v10()
    before = connection.execute("SELECT * FROM account").fetchall()
    schema.initialize_schema(connection, initial_balance="999999")
    assert connection.execute("SELECT * FROM account").fetchall() == before
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rollover_states", "rollover_transitions", "rollover_commands"} <= tables
    schema.initialize_schema(connection)
    connection.close()


def test_v11_ddl_failure_is_atomic(monkeypatch):
    connection = legacy_v10()
    monkeypatch.setattr(schema, "MIGRATE_V10_TO_V11_SQL", schema.MIGRATE_V10_TO_V11_SQL+"CREATE TABLE rollover_states(bad INT);")
    with pytest.raises(sqlite3.OperationalError):
        schema.initialize_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='rollover_states'").fetchone() is None
    assert connection.execute("SELECT balance FROM account").fetchone()[0] == "99888"
    connection.close()
