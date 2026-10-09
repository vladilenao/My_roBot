"""Additive v9→v10: сохранение фактов, ограничений и атомарный rollback DDL."""

import sqlite3

import pytest

import src.trade_journal.schema as schema


def _legacy(connection):
    connection.executescript(schema.SCHEMA_V9_SQL)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA user_version=9")
    connection.execute("INSERT INTO export_state(export_id,updated_at) VALUES(1,'2026-01-01')")
    connection.execute("INSERT INTO trades VALUES('t','a','instrument','signal','BUY',"
                       "'{\"reference_entry\":\"100\",\"stop_price\":\"96\",\"targets\":[]}',"
                       "'{\"name\":\"levels_rr\",\"version\":\"1\",\"parameters\":{}}',"
                       "'OPEN',1,'{}','2026-01-01','2026-01-01','1','100')")
    connection.execute("INSERT INTO positions VALUES('t','BUY',1,'100','-3','2','-5','2026-01-01')")
    connection.execute("INSERT INTO account VALUES(1,'99995','99990','-3','2','-5','2026-01-01')")
    connection.execute("INSERT INTO outbox VALUES('cmd','t','{}','SENT','2026-01-01','2026-01-01')")
    connection.execute("INSERT INTO orders VALUES('order','t','cmd','OPEN','PARTIAL',2,1,'100','2026-01-01','2026-01-01')")
    connection.execute("INSERT INTO fills VALUES('fill','order','t','cmd','exec',1,'100','2','2026-01-01')")
    connection.execute("INSERT INTO protection VALUES('t','96',NULL,NULL,NULL,'2026-01-01')")
    connection.execute("INSERT INTO targets VALUES('tp','t',0,'104',1,0,'PENDING')")
    connection.execute("INSERT INTO reservations VALUES('r','t','order','400','100','800','200','ACTIVE','2026-01-01','2026-01-01')")
    connection.commit()


def test_migration_preserves_money_and_active_state_without_ruble_reset():
    connection = sqlite3.connect(":memory:")
    _legacy(connection)
    before = {table: connection.execute(f"SELECT * FROM {table}").fetchall()
              for table in ("trades", "positions", "account", "orders", "protection", "targets", "reservations")}
    schema.initialize_schema(connection, initial_balance="123456")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    for table, rows in before.items():
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
    assert connection.execute("SELECT fee,fee_source,reference_price,slippage_amount FROM fills").fetchone() == ("2", "unknown", None, None)
    assert connection.execute("SELECT COUNT(*) FROM trade_measurements").fetchone()[0] == 0
    schema.initialize_schema(connection)
    connection.close()


def test_failure_after_alter_rolls_back_columns_tables_and_user_version(monkeypatch):
    connection = sqlite3.connect(":memory:")
    _legacy(connection)
    monkeypatch.setattr(schema, "MIGRATE_V9_TO_V10_SQL", schema.MIGRATE_V9_TO_V10_SQL + "\nCREATE TABLE cost_adjustments(bad INT);\n")
    with pytest.raises(sqlite3.OperationalError):
        schema.initialize_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "fee_source" not in {r[1] for r in connection.execute("PRAGMA table_info(fills)")}
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='cost_adjustments'").fetchone() is None
    assert connection.execute("SELECT balance FROM account").fetchone()[0] == "99995"
    connection.close()


def test_migration_inside_callers_transaction_does_not_commit_it():
    connection = sqlite3.connect(":memory:")
    _legacy(connection)
    connection.execute("BEGIN")
    connection.execute("UPDATE account SET balance='99994'")
    schema.initialize_schema(connection)
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert connection.execute("SELECT balance FROM account").fetchone()[0] == "99995"
    connection.close()


def test_current_schema_has_new_tables_and_rejects_future_versions():
    connection = sqlite3.connect(":memory:")
    schema.initialize_schema(connection)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cost_adjustments", "trade_measurements", "trade_market_observations"} <= tables
    connection.execute("PRAGMA user_version=11")
    with pytest.raises(schema.UnsupportedSchemaVersion):
        schema.initialize_schema(connection)
    connection.close()


def test_v9_reader_refuses_v10_without_mutating_the_database(monkeypatch):
    connection = sqlite3.connect(":memory:")
    schema.initialize_schema(connection)
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 9)
    with pytest.raises(schema.UnsupportedSchemaVersion):
        schema.initialize_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    connection.close()
