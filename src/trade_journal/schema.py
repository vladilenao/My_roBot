"""Versioned SQLite schema for the trade journal source of truth."""

from __future__ import annotations

import sqlite3
from decimal import Decimal


SCHEMA_VERSION = 9


class UnsupportedSchemaVersion(RuntimeError):
    """Raised when a database cannot safely be opened by this application."""


SCHEMA_SQL = """
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
    price_step TEXT,
    step_cost TEXT,
    UNIQUE (assignment_id, instrument_id, trade_id)
);
CREATE UNIQUE INDEX active_trade_per_assignment_instrument
    ON trades (assignment_id, instrument_id)
    WHERE phase NOT IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR');

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

CREATE TABLE outbox (
    command_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    command_id TEXT NOT NULL UNIQUE REFERENCES outbox(command_id) ON DELETE RESTRICT,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    requested_price TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE RESTRICT,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE RESTRICT,
    command_id TEXT NOT NULL REFERENCES orders(command_id) ON DELETE RESTRICT,
    execution_id TEXT NOT NULL UNIQUE,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price TEXT NOT NULL,
    fee TEXT NOT NULL DEFAULT '0',
    executed_at TEXT NOT NULL
);

CREATE TABLE targets (
    target_id TEXT NOT NULL,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    target_index INTEGER NOT NULL CHECK (target_index >= 0),
    price TEXT NOT NULL,
    planned_quantity INTEGER NOT NULL DEFAULT 0 CHECK (planned_quantity >= 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    status TEXT NOT NULL,
    PRIMARY KEY (trade_id, target_id),
    UNIQUE (trade_id, target_index)
);

CREATE TABLE protection (
    trade_id TEXT PRIMARY KEY REFERENCES trades(trade_id) ON DELETE CASCADE,
    confirmed_stop TEXT,
    pending_stop TEXT,
    confirmed_order_id TEXT REFERENCES orders(order_id) ON DELETE SET NULL,
    pending_command_id TEXT REFERENCES outbox(command_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
    risk_amount TEXT NOT NULL,
    margin_amount TEXT NOT NULL,
    original_risk_amount TEXT NOT NULL,
    original_margin_amount TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
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

CREATE TABLE events (
    event_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    trade_id TEXT REFERENCES trades(trade_id) ON DELETE SET NULL,
    order_id TEXT REFERENCES orders(order_id) ON DELETE SET NULL,
    command_id TEXT REFERENCES outbox(command_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE processed_signals (
    assignment_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    trade_id TEXT REFERENCES trades(trade_id) ON DELETE SET NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (assignment_id, signal_id)
);

CREATE TABLE market_inputs (
    source_data_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    seed_json TEXT,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE calculations (
    calculation_id TEXT PRIMARY KEY,
    source_data_id TEXT REFERENCES market_inputs(source_data_id) ON DELETE RESTRICT,
    trade_id TEXT REFERENCES trades(trade_id) ON DELETE SET NULL,
    command_id TEXT REFERENCES outbox(command_id) ON DELETE SET NULL,
    event_id TEXT REFERENCES events(event_id) ON DELETE SET NULL,
    assignment_id TEXT,
    signal_id TEXT,
    service_uid TEXT,
    correlation_id TEXT,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    input_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    rounding_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ACCEPTED', 'REJECTED')),
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_exports (
    calculation_id TEXT PRIMARY KEY REFERENCES calculations(calculation_id) ON DELETE CASCADE,
    exported_at TEXT NOT NULL
);

CREATE TABLE export_state (
    export_id INTEGER PRIMARY KEY CHECK (export_id = 1),
    required_revision INTEGER NOT NULL DEFAULT 0 CHECK (required_revision >= 0),
    exported_revision INTEGER NOT NULL DEFAULT 0 CHECK (exported_revision >= 0),
    audit_exported_revision INTEGER NOT NULL DEFAULT 0 CHECK (audit_exported_revision >= 0),
    failed_revision INTEGER CHECK (failed_revision >= 0),
    last_error TEXT,
    audit_failed_revision INTEGER CHECK (audit_failed_revision >= 0),
    audit_last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE instrument_names (
    ticker TEXT PRIMARY KEY,
    short_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

MIGRATE_V1_TO_V2_SQL = """
ALTER TABLE calculations ADD COLUMN assignment_id TEXT;
ALTER TABLE calculations ADD COLUMN signal_id TEXT;
ALTER TABLE calculations ADD COLUMN service_uid TEXT;
ALTER TABLE calculations ADD COLUMN correlation_id TEXT;
ALTER TABLE calculations ADD COLUMN rounding_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE calculations ADD COLUMN outcome TEXT NOT NULL DEFAULT 'ACCEPTED'
    CHECK (outcome IN ('ACCEPTED', 'REJECTED'));
"""

MIGRATE_V2_TO_V3_SQL = """
ALTER TABLE reservations ADD COLUMN original_risk_amount TEXT NOT NULL DEFAULT '0';
ALTER TABLE reservations ADD COLUMN original_margin_amount TEXT NOT NULL DEFAULT '0';
UPDATE reservations SET original_risk_amount = risk_amount, original_margin_amount = margin_amount;
"""

MIGRATE_V3_TO_V4_SQL = """
ALTER TABLE positions ADD COLUMN net_realized_pnl TEXT NOT NULL DEFAULT '0';
ALTER TABLE account ADD COLUMN net_realized_pnl TEXT NOT NULL DEFAULT '0';
"""

MIGRATE_V4_TO_V5_SQL = """
ALTER TABLE export_state ADD COLUMN failed_revision INTEGER CHECK (failed_revision >= 0);
ALTER TABLE export_state ADD COLUMN last_error TEXT;
"""

MIGRATE_V5_TO_V6_SQL = """
CREATE TABLE audit_exports (
    calculation_id TEXT PRIMARY KEY REFERENCES calculations(calculation_id) ON DELETE CASCADE,
    exported_at TEXT NOT NULL
);
ALTER TABLE export_state ADD COLUMN audit_failed_revision INTEGER CHECK (audit_failed_revision >= 0);
ALTER TABLE export_state ADD COLUMN audit_last_error TEXT;
"""

MIGRATE_V6_TO_V7_SQL = """
CREATE TABLE targets_new (
    target_id TEXT NOT NULL,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    target_index INTEGER NOT NULL CHECK (target_index >= 0),
    price TEXT NOT NULL,
    planned_quantity INTEGER NOT NULL DEFAULT 0 CHECK (planned_quantity >= 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    status TEXT NOT NULL,
    PRIMARY KEY (trade_id, target_id),
    UNIQUE (trade_id, target_index)
);
INSERT INTO targets_new (target_id, trade_id, target_index, price, planned_quantity, filled_quantity, status)
    SELECT target_id, trade_id, target_index, price, planned_quantity, filled_quantity, status FROM targets;
DROP TABLE targets;
ALTER TABLE targets_new RENAME TO targets;
"""

MIGRATE_V7_TO_V8_SQL = """
ALTER TABLE trades ADD COLUMN price_step TEXT;
ALTER TABLE trades ADD COLUMN step_cost TEXT;
"""

MIGRATE_V8_TO_V9_SQL = """
CREATE TABLE instrument_names (
    ticker TEXT PRIMARY KEY,
    short_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def initialize_schema(connection: sqlite3.Connection, *, initial_balance: str | None = None) -> None:
    """Create the current schema or reject a database from another version.

    ``initial_balance`` задаёт рублёвое переоснование счёта (ruble epoch) при
    миграции на версию 8: legacy-сделки остаются в прежних единицах PnL и их
    агрегаты больше не смешиваются со счётом. Если ``None`` — выполняется
    только изменение схемы без сброса счёта.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, SCHEMA_VERSION):
        raise UnsupportedSchemaVersion(
            f"unsupported SQLite schema version {version}; expected {SCHEMA_VERSION}"
        )

    if version == 0:
        existing_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        if existing_tables:
            raise UnsupportedSchemaVersion(
                "unversioned SQLite database contains tables and cannot be opened"
            )
        with connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT INTO export_state (export_id, updated_at) VALUES (1, datetime('now'))"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 1:
        with connection:
            connection.executescript(MIGRATE_V1_TO_V2_SQL)
            connection.executescript(MIGRATE_V2_TO_V3_SQL)
            connection.executescript(MIGRATE_V3_TO_V4_SQL)
            connection.executescript(MIGRATE_V4_TO_V5_SQL)
            connection.executescript(MIGRATE_V5_TO_V6_SQL)
            _backfill_net_realized_pnl(connection)
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 2:
        with connection:
            connection.executescript(MIGRATE_V2_TO_V3_SQL)
            connection.executescript(MIGRATE_V3_TO_V4_SQL)
            connection.executescript(MIGRATE_V4_TO_V5_SQL)
            connection.executescript(MIGRATE_V5_TO_V6_SQL)
            _backfill_net_realized_pnl(connection)
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 3:
        with connection:
            connection.executescript(MIGRATE_V3_TO_V4_SQL)
            connection.executescript(MIGRATE_V4_TO_V5_SQL)
            connection.executescript(MIGRATE_V5_TO_V6_SQL)
            _backfill_net_realized_pnl(connection)
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 4:
        with connection:
            connection.executescript(MIGRATE_V4_TO_V5_SQL)
            connection.executescript(MIGRATE_V5_TO_V6_SQL)
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 5:
        with connection:
            connection.executescript(MIGRATE_V5_TO_V6_SQL)
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 6:
        with connection:
            connection.executescript(MIGRATE_V6_TO_V7_SQL)
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 7:
        with connection:
            _migrate_v7_to_v8(connection, initial_balance)
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 8:
        with connection:
            _migrate_v8_to_v9(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(f"foreign key integrity check failed: {violations!r}")


def _migrate_v7_to_v8(connection: sqlite3.Connection, initial_balance: str | None) -> None:
    """Перенести v7 → v8: снапшот факторов контракта и рублёвая эпоха счёта."""
    connection.executescript(MIGRATE_V7_TO_V8_SQL)
    if initial_balance is None:
        return
    connection.execute(
        "UPDATE account SET balance = ?, equity = ?, realized_pnl = '0', fees = '0', "
        "net_realized_pnl = '0', updated_at = datetime('now') WHERE account_id = 1",
        (initial_balance, initial_balance),
    )


def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
    """Перенести v8 → v9: сохраняемая карта коротких имён контрактов."""
    connection.executescript(MIGRATE_V8_TO_V9_SQL)


def _backfill_net_realized_pnl(connection: sqlite3.Connection) -> None:
    """Backfill decimal PnL exactly rather than through SQLite floating arithmetic."""
    for table, key in (("positions", "trade_id"), ("account", "account_id")):
        rows = connection.execute(f"SELECT {key}, realized_pnl, fees FROM {table}").fetchall()
        connection.executemany(
            f"UPDATE {table} SET net_realized_pnl = ? WHERE {key} = ?",
            [(format(Decimal(gross) - Decimal(fees), "f"), identifier)
             for identifier, gross, fees in rows],
        )
