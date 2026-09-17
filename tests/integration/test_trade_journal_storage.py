import sqlite3

import pytest

from src.trade_journal.schema import SCHEMA_VERSION, UnsupportedSchemaVersion
from src.trade_journal.storage import BUSY_TIMEOUT_MS, connect


def _trade(connection, trade_id="trade-1"):
    connection.execute(
        """
        INSERT INTO trades (
            trade_id, assignment_id, instrument_id, signal_id, side, plan_json,
            profile_json, phase, created_at, updated_at
        ) VALUES (?, 'assignment-1', 'instrument-1', 'signal-1', 'BUY', '{}', '{}',
                  'PLANNED', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (trade_id,),
    )


def test_new_database_has_current_schema_and_required_pragmas(tmp_path):
    connection = connect(tmp_path / "trades.sqlite3")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert {
            "trades", "positions", "orders", "fills", "targets", "protection",
            "reservations", "account", "events", "processed_signals", "outbox",
            "calculations", "market_inputs", "export_state",
        } <= tables
    finally:
        connection.close()


def test_unknown_schema_version_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(UnsupportedSchemaVersion, match="999"):
        connect(path)


def test_foreign_keys_and_idempotency_keys_are_enforced(tmp_path):
    connection = connect(tmp_path / "trades.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO positions (trade_id, side, updated_at) VALUES "
                "('missing', 'BUY', '2026-01-01T00:00:00Z')"
            )

        _trade(connection)
        connection.execute(
            """
            INSERT INTO outbox (command_id, trade_id, payload_json, status, created_at)
            VALUES ('command-1', 'trade-1', '{}', 'PENDING', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO orders (
                order_id, trade_id, command_id, action_type, status, quantity,
                created_at, updated_at
            ) VALUES ('order-1', 'trade-1', 'command-1', 'OPEN', 'PENDING', 1,
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO fills (
                fill_id, order_id, trade_id, command_id, execution_id, quantity,
                price, executed_at
            ) VALUES ('fill-1', 'order-1', 'trade-1', 'command-1', 'execution-1',
                      1, '100', '2026-01-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO outbox (command_id, trade_id, payload_json, status, created_at)
                VALUES ('command-2', 'trade-1', '{}', 'PENDING', '2026-01-01T00:00:00Z')
                """
            )
            connection.execute(
                "INSERT INTO orders (order_id, trade_id, command_id, action_type, "
                "status, quantity, created_at, updated_at) VALUES "
                "('order-2', 'trade-1', 'command-1', 'OPEN', 'PENDING', 1, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO fills (fill_id, order_id, trade_id, command_id, "
                "execution_id, quantity, price, executed_at) VALUES "
                "('fill-2', 'order-1', 'trade-1', 'command-1', 'execution-1', 1, "
                "'100', '2026-01-01T00:00:00Z')"
            )
        connection.execute(
            "INSERT INTO processed_signals (assignment_id, signal_id, processed_at) "
            "VALUES ('assignment-1', 'signal-1', '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO processed_signals (assignment_id, signal_id, processed_at) "
                "VALUES ('assignment-1', 'signal-1', '2026-01-01T00:00:00Z')"
            )
        connection.execute(
            "INSERT INTO events (event_id, event_type, payload_json, occurred_at) "
            "VALUES ('event-1', 'fill', '{}', '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO events (event_id, event_type, payload_json, occurred_at) "
                "VALUES ('event-1', 'fill', '{}', '2026-01-01T00:00:00Z')"
            )
    finally:
        connection.close()
