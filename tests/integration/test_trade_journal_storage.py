import sqlite3

import pytest

from src.trade_journal.schema import SCHEMA_VERSION, UnsupportedSchemaVersion
from src.trade_journal.storage import BUSY_TIMEOUT_MS, connect

V7_SCHEMA_SQL = """
CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    plan_json TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    phase TEXT NOT NULL,
    state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    profile_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (assignment_id, instrument_id, trade_id)
);
CREATE TABLE positions (
    trade_id TEXT PRIMARY KEY REFERENCES trades(trade_id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    average_price TEXT,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    net_realized_pnl TEXT NOT NULL DEFAULT '0',
    updated_at TEXT NOT NULL
);
CREATE TABLE account (
    account_id INTEGER PRIMARY KEY CHECK (account_id = 1),
    balance TEXT NOT NULL,
    equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    net_realized_pnl TEXT NOT NULL DEFAULT '0',
    updated_at TEXT NOT NULL
);
"""


def _make_v7_database(path, *, balance: str = "1000") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(V7_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, plan_json, "
        "profile_json, phase, state_revision, profile_state_json, created_at, updated_at) "
        "VALUES ('legacy-1', 'assignment-1', 'NGV6', 'signal-1', 'BUY', '{}', '{}', 'CLOSED', 3, "
        "'{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO positions (trade_id, side, quantity, average_price, realized_pnl, fees, "
        "net_realized_pnl, updated_at) VALUES ('legacy-1', 'BUY', 0, NULL, '15', '3', '12', "
        "'2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO account (account_id, balance, equity, realized_pnl, fees, net_realized_pnl, "
        "updated_at) VALUES (1, ?, ?, '15', '3', '12', '2026-01-01T00:00:00Z')",
        (balance, balance),
    )
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()


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


def test_migration_v7_to_v8_rebases_account_when_initial_deposit_given(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _make_v7_database(path, balance="100000")
    connection = connect(path, initial_deposit="250000")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trades)")}
        assert {"price_step", "step_cost"} <= columns
        legacy = connection.execute(
            "SELECT price_step, step_cost FROM trades WHERE trade_id = 'legacy-1'"
        ).fetchone()
        assert legacy == (None, None)
        row = connection.execute(
            "SELECT balance, equity, realized_pnl, fees, net_realized_pnl FROM account"
        ).fetchone()
        assert row == ("250000", "250000", "0", "0", "0")
    finally:
        connection.close()


def test_migration_v7_to_v8_without_initial_deposit_keeps_account(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _make_v7_database(path, balance="100000")
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trades)")}
        assert {"price_step", "step_cost"} <= columns
        legacy = connection.execute(
            "SELECT price_step, step_cost FROM trades WHERE trade_id = 'legacy-1'"
        ).fetchone()
        assert legacy == (None, None)
        row = connection.execute(
            "SELECT balance, realized_pnl, fees, net_realized_pnl FROM account"
        ).fetchone()
        assert row == ("100000", "15", "3", "12")
    finally:
        connection.close()
