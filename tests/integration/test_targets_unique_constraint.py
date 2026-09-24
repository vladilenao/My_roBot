import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.schema import SCHEMA_SQL, SCHEMA_VERSION
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePhase, TradePlan


NOW = datetime(2026, 1, 1, tzinfo=UTC)


class EntryBroker(BrokerPort):
    """Synchronous deterministic executor: entries fill, nothing else is used."""

    def submit(self, action, now):
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


def _plan(trade_id, assignment_id, ticker, signal_id):
    return TradePlan(
        trade_id, assignment_id, ticker, "BUY", signal_id, Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("0.5")),
         TargetPlan("tp-2", Decimal("108"), Decimal("0.5"))),
        ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW,
    )


def _open_trade(manager, plan, command_id):
    manager.submit_plan(plan, OpenTrade(command_id, plan.trade_id, 0, "entry", 1))
    manager.dispatch(NOW)


def test_two_trades_coexist_with_same_target_ids(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, EntryBroker(), initial_balance=Decimal("10000"))
        first = _plan("trade-1", "assignment-1", "NGV6", "signal-1")
        second = _plan("trade-2", "assignment-2", "BRV6", "signal-2")
        _open_trade(manager, first, "open-1")
        _open_trade(manager, second, "open-2")

        rows = storage.connection.execute(
            "SELECT trade_id, target_id, target_index, status FROM targets "
            "ORDER BY trade_id, target_index"
        ).fetchall()
        assert rows == [
            ("trade-1", "tp-1", 0, "PENDING"),
            ("trade-1", "tp-2", 1, "PENDING"),
            ("trade-2", "tp-1", 0, "PENDING"),
            ("trade-2", "tp-2", 1, "PENDING"),
        ]
        phases = storage.connection.execute(
            "SELECT trade_id, phase FROM trades ORDER BY trade_id"
        ).fetchall()
        assert phases == [("trade-1", TradePhase.OPEN.value), ("trade-2", TradePhase.OPEN.value)]


V6_TARGETS_DDL = """
CREATE TABLE targets (
    target_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    target_index INTEGER NOT NULL CHECK (target_index >= 0),
    price TEXT NOT NULL,
    planned_quantity INTEGER NOT NULL DEFAULT 0 CHECK (planned_quantity >= 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    status TEXT NOT NULL,
    UNIQUE (trade_id, target_index)
);
"""


def _open_v6_database(path):
    """БД с текущим наполнением таблиц и целями v6 (глобальный target_id PK)."""
    connection = sqlite3.connect(path)
    with connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("DROP TABLE targets")
        connection.executescript(V6_TARGETS_DDL)
        connection.execute(
            """
            INSERT INTO trades (
                trade_id, assignment_id, instrument_id, signal_id, side, plan_json,
                profile_json, phase, created_at, updated_at
            ) VALUES ('trade-legacy', 'assignment-legacy', 'NGV6', 'signal-legacy', 'BUY',
                      '{}', '{}', 'OPEN', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            "INSERT INTO targets VALUES ('tp-1', 'trade-legacy', 0, '104', 1, 1, 'FILLED')"
        )
        connection.execute(
            "INSERT INTO targets VALUES ('tp-2', 'trade-legacy', 1, '108', 1, 0, 'PENDING')"
        )
        connection.execute("PRAGMA user_version = 6")
    connection.close()


def test_migration_v6_to_v7_preserves_targets_and_opens_next_trade(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _open_v6_database(database)

    with Storage(database) as storage:
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        rows = storage.connection.execute(
            "SELECT target_id, trade_id, target_index, price, planned_quantity, "
            "filled_quantity, status FROM targets ORDER BY target_index"
        ).fetchall()
        assert rows == [
            ("tp-1", "trade-legacy", 0, "104", 1, 1, "FILLED"),
            ("tp-2", "trade-legacy", 1, "108", 1, 0, "PENDING"),
        ]

        manager = TradeManager(storage, EntryBroker(), initial_balance=Decimal("10000"))
        _open_trade(manager, _plan("trade-new", "assignment-new", "BRV6", "signal-new"), "open-new")

        assert storage.connection.execute(
            "SELECT COUNT(*) FROM targets WHERE trade_id = 'trade-new'"
        ).fetchone()[0] == 2
        phase = storage.connection.execute(
            "SELECT phase FROM trades WHERE trade_id = 'trade-new'"
        ).fetchone()[0]
        assert phase == TradePhase.OPEN.value