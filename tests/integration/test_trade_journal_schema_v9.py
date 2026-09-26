import sqlite3

import pytest

from src.trade_journal.schema import SCHEMA_VERSION, UnsupportedSchemaVersion, initialize_schema
from src.trade_journal.storage import Storage


def _make_v8_database(path) -> None:
    connection = sqlite3.connect(path)
    initialize_schema(connection)
    connection.executescript(
        "DROP TABLE instrument_names;"
        "PRAGMA user_version = 8;"
    )
    connection.execute(
        "INSERT INTO account VALUES (1, '100000', '95000', '10', '-2', '8', '2026-09-24 12:00:00')"
    )
    connection.execute(
        "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, plan_json, "
        "profile_json, phase, created_at, updated_at) "
        "VALUES ('t1', 'a1', 'NGV6', 's1', 'BUY', '{}', '{}', 'OPEN', "
        "'2026-09-24T12:00:00', '2026-09-24T12:00:00')"
    )
    connection.commit()
    connection.close()


def test_v8_database_is_migrated_to_v9_with_name_table(tmp_path):
    database = tmp_path / "trades.sqlite3"
    _make_v8_database(database)

    with Storage(database) as storage:
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION == 9
    assert "instrument_names" in tables


def test_migration_preserves_existing_account_and_trades(tmp_path):
    database = tmp_path / "trades.sqlite3"
    _make_v8_database(database)

    with Storage(database) as storage:
        account = storage.connection.execute(
            "SELECT balance, equity, realized_pnl, net_realized_pnl FROM account"
        ).fetchone()
        trades = storage.connection.execute("SELECT count(*) FROM trades").fetchone()[0]

    assert account == ("100000", "95000", "10", "8")
    assert trades == 1


def test_name_map_is_empty_until_the_bot_passes_it(tmp_path):
    database = tmp_path / "trades.sqlite3"
    _make_v8_database(database)

    with Storage(database) as storage:
        assert storage.instrument_names() == {}

        storage.set_names({"NGV6": "NG-10.26"})

        assert storage.instrument_names() == {"NGV6": "NG-10.26"}


def test_database_from_a_newer_version_is_rejected(tmp_path):
    database = tmp_path / "trades.sqlite3"
    connection = sqlite3.connect(database)
    initialize_schema(connection)
    connection.execute("PRAGMA user_version = 10")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedSchemaVersion):
        Storage(database)
