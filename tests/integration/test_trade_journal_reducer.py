from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import Storage


class ControlledExecutor:
    """Delivers broker outcomes in a prescribed order, including late outcomes."""

    def __init__(self, *events: ExecutionEvent) -> None:
        self._events = iter(events)

    def next(self) -> ExecutionEvent:
        return next(self._events)


def _seed(storage, phase="OPEN"):
    connection = storage.connection
    now = "2026-01-01T00:00:00+00:00"
    connection.execute("INSERT INTO trades VALUES ('trade-1', 'assignment-1', 'NGV6', 'signal-1', 'BUY', '{}', '{}', ?, 0, '{}', ?, ?)", (phase, now, now))
    connection.execute("INSERT INTO positions VALUES ('trade-1', 'BUY', 0, NULL, '0', '0', '0', ?)", (now,))
    connection.execute("INSERT INTO account VALUES (1, '1000', '1000', '0', '0', '0', ?)", (now,))
    connection.execute("INSERT INTO outbox VALUES ('command-1', 'trade-1', '{}', 'SENT', ?, NULL)", (now,))
    connection.execute("INSERT INTO orders VALUES ('order-1', 'trade-1', 'command-1', 'OPEN', 'PENDING', 2, 0, NULL, ?, ?)", (now, now))
    connection.execute(
        "INSERT INTO reservations VALUES ('reservation-1', 'trade-1', 'order-1', '20', '40', '20', '40', 'ACTIVE', ?, ?)",
        (now, now),
    )
    connection.execute("INSERT INTO targets VALUES ('target-1', 'trade-1', 0, '104', 1, 0, 'PENDING')")
    connection.commit()


def _event(execution_id="execution-1"):
    return ExecutionEvent(execution_id, "order-1", "command-1", "trade-1", ExecutionStatus.PARTIAL, 1,
                          Decimal("100.125"), Decimal("0.5"), datetime.now(timezone.utc), "broker-fill")


def test_execution_is_atomic_and_duplicate_does_not_mutate_twice(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed(storage)
        reducer = ExecutionReducer(storage)

        assert reducer.apply(_event()) is True
        assert reducer.apply(_event()) is False

        position = storage.connection.execute("SELECT quantity, average_price, fees FROM positions").fetchone()
        account = storage.connection.execute("SELECT balance, fees FROM account").fetchone()
        reservation = storage.connection.execute("SELECT risk_amount, margin_amount FROM reservations").fetchone()
        assert position == (1, "100.125", "0.5")
        assert account == ("999.5", "0.5")
        assert tuple(map(Decimal, reservation)) == (Decimal("10"), Decimal("20"))
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_failed_event_append_rolls_back_all_reducer_changes(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed(storage)
        storage.connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
        reducer = ExecutionReducer(storage)

        with pytest.raises(Exception, match="forced failure"):
            reducer.apply(_event())

        assert storage.connection.execute("SELECT quantity, average_price, fees FROM positions").fetchone() == (0, None, "0")
        assert storage.connection.execute("SELECT balance, fees FROM account").fetchone() == ("1000", "0")
        assert storage.connection.execute("SELECT risk_amount, margin_amount FROM reservations").fetchone() == ("20", "40")
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_target_fill_updates_only_its_target_and_realizes_position_pnl(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed(storage)
        reducer = ExecutionReducer(storage)
        reducer.apply(_event())
        now = "2026-01-01T00:00:00+00:00"
        storage.connection.execute("INSERT INTO outbox VALUES ('command-2', 'trade-1', '{}', 'SENT', ?, NULL)", (now,))
        storage.connection.execute("INSERT INTO orders VALUES ('order-2', 'trade-1', 'command-2', 'TARGET:target-1', 'PENDING', 1, 0, NULL, ?, ?)", (now, now))
        storage.connection.commit()

        assert reducer.apply(ExecutionEvent(
            "execution-2", "order-2", "command-2", "trade-1", ExecutionStatus.FILL, 1,
            Decimal("104"), Decimal("0.25"), datetime.now(timezone.utc), "target-hit",
        )) is True

        assert storage.connection.execute("SELECT quantity, realized_pnl FROM positions").fetchone() == (0, "3.875")
        assert storage.connection.execute("SELECT filled_quantity, status FROM targets WHERE target_id='target-1'").fetchone() == (1, "FILLED")


def test_every_fill_fee_is_accumulated_once_and_net_pnl_is_separate(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed(storage)
        reducer = ExecutionReducer(storage)
        reducer.apply(_event())
        now = "2026-01-01T00:00:00+00:00"
        storage.connection.execute("INSERT INTO outbox VALUES ('command-2', 'trade-1', '{}', 'SENT', ?, NULL)", (now,))
        storage.connection.execute("INSERT INTO orders VALUES ('order-2', 'trade-1', 'command-2', 'TARGET:target-1', 'PENDING', 1, 0, NULL, ?, ?)", (now, now))
        storage.connection.commit()
        exit_fill = ExecutionEvent(
            "execution-2", "order-2", "command-2", "trade-1", ExecutionStatus.FILL, 1,
            Decimal("104"), Decimal("0.25"), datetime.now(timezone.utc), "target-hit",
        )

        assert reducer.apply(exit_fill)
        assert not reducer.apply(exit_fill)
        assert storage.connection.execute(
            "SELECT realized_pnl, fees, net_realized_pnl FROM account"
        ).fetchone() == ("3.875", "0.75", "3.125")
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_controlled_executor_keeps_phase_and_target_pending_until_late_fills(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed(storage, phase="ENTRY_PENDING")
        now = datetime.now(timezone.utc)
        storage.connection.execute(
            "INSERT INTO outbox VALUES ('command-target', 'trade-1', '{}', 'SENT', ?, NULL)",
            (now.isoformat(),),
        )
        storage.connection.execute(
            "INSERT INTO orders VALUES ('order-target', 'trade-1', 'command-target', 'TARGET:target-1', 'PENDING', 2, 0, NULL, ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        storage.connection.execute("UPDATE targets SET planned_quantity = 2 WHERE target_id = 'target-1'")
        storage.connection.commit()
        executor = ControlledExecutor(
            ExecutionEvent("entry-partial", "order-1", "command-1", "trade-1", ExecutionStatus.PARTIAL, 1,
                           Decimal("100"), Decimal("0"), now, "partial"),
            ExecutionEvent("entry-cancel", "order-1", "command-1", "trade-1", ExecutionStatus.CANCEL, 0,
                           None, Decimal("0"), now, "cancelled-remainder"),
            ExecutionEvent("entry-late-fill", "order-1", "command-1", "trade-1", ExecutionStatus.FILL, 1,
                           Decimal("100"), Decimal("0"), now, "late-fill"),
            ExecutionEvent("target-reject", "order-target", "command-target", "trade-1", ExecutionStatus.REJECT, 0,
                           None, Decimal("0"), now, "venue-reject"),
            ExecutionEvent("target-cancel", "order-target", "command-target", "trade-1", ExecutionStatus.CANCEL, 0,
                           None, Decimal("0"), now, "cancelled"),
            ExecutionEvent("target-late-partial", "order-target", "command-target", "trade-1", ExecutionStatus.PARTIAL, 1,
                           Decimal("104"), Decimal("0"), now, "late-fill"),
            ExecutionEvent("target-fill", "order-target", "command-target", "trade-1", ExecutionStatus.FILL, 1,
                           Decimal("104"), Decimal("0"), now, "confirmed-fill"),
        )
        reducer = ExecutionReducer(storage)

        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT phase FROM trades").fetchone()[0] == "OPEN"
        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT phase FROM trades").fetchone()[0] == "OPEN"
        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT quantity, phase FROM positions JOIN trades USING (trade_id)").fetchone() == (2, "OPEN")

        assert reducer.apply(executor.next()) is True
        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT status FROM targets").fetchone()[0] == "PENDING"
        assert storage.connection.execute("SELECT phase FROM trades").fetchone()[0] == "OPEN"

        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT filled_quantity, status FROM targets").fetchone() == (1, "PARTIAL")
        assert storage.connection.execute("SELECT phase FROM trades").fetchone()[0] == "REDUCING"
        assert reducer.apply(executor.next()) is True
        assert storage.connection.execute("SELECT filled_quantity, status FROM targets").fetchone() == (2, "FILLED")
        assert storage.connection.execute("SELECT quantity, phase FROM positions JOIN trades USING (trade_id)").fetchone() == (0, "CLOSED")
