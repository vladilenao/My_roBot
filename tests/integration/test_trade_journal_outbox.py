from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import Storage


def _seed_trade(storage: Storage) -> None:
    now = "2026-01-01T00:00:00+00:00"
    storage.connection.execute(
        "INSERT INTO trades VALUES ('trade-1', 'assignment-1', 'NGV6', 'signal-1', "
        "'BUY', '{}', '{}', 'OPEN', 0, '{}', ?, ?, NULL, NULL)",
        (now, now),
    )
    storage.connection.execute(
        "INSERT INTO positions VALUES ('trade-1', 'BUY', 0, NULL, '0', '0', '0', ?)", (now,)
    )
    storage.connection.execute(
        "INSERT INTO account VALUES (1, '1000', '1000', '0', '0', '0', ?)", (now,)
    )
    storage.connection.commit()


def test_signal_and_outbox_roll_back_together_before_commit(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed_trade(storage)
        storage.connection.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )

        with pytest.raises(Exception, match="forced failure"):
            storage.record_signal_and_enqueue(
                "assignment-1", "signal-2", "command-1", "trade-1", {"kind": "open"}
            )

        assert storage.connection.execute("SELECT COUNT(*) FROM processed_signals").fetchone()[0] == 0
        assert storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_command_is_redelivered_after_post_commit_crash_but_not_after_mark_sent(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        _seed_trade(storage)
        assert storage.record_signal_and_enqueue(
            "assignment-1", "signal-2", "command-1", "trade-1", {"kind": "open"}
        )
        assert not storage.record_signal_and_enqueue(
            "assignment-1", "signal-2", "command-2", "trade-1", {"kind": "open"}
        )
        assert [command.command_id for command in storage.claim_outbox()] == ["command-1"]
        # A broker may have received command-1 here; no sent mark simulates a crash.

    with Storage(path) as restarted:
        assert [command.command_id for command in restarted.claim_outbox()] == ["command-1"]
        assert restarted.mark_outbox_sent("command-1")

    with Storage(path) as restarted_again:
        assert restarted_again.claim_outbox() == ()
        assert restarted_again.connection.execute(
            "SELECT status FROM outbox WHERE command_id = 'command-1'"
        ).fetchone()[0] == "SENT"


def test_identical_fill_is_ignored_after_restart(tmp_path):
    path = tmp_path / "trades.sqlite3"
    event = ExecutionEvent(
        "execution-1", "order-1", "command-1", "trade-1", ExecutionStatus.FILL, 1,
        Decimal("100"), Decimal("0.5"), datetime.now(timezone.utc), "broker-fill",
    )
    with Storage(path) as storage:
        _seed_trade(storage)
        storage.enqueue("command-1", "trade-1", {"kind": "open"})
        now = "2026-01-01T00:00:00+00:00"
        storage.connection.execute(
            "INSERT INTO orders VALUES ('order-1', 'trade-1', 'command-1', 'OPEN', "
            "'PENDING', 1, 0, NULL, ?, ?)",
            (now, now),
        )
        storage.connection.commit()
        assert ExecutionReducer(storage).apply(event)

    with Storage(path) as restarted:
        assert not ExecutionReducer(restarted).apply(event)
        assert restarted.connection.execute("SELECT quantity FROM positions").fetchone()[0] == 1
        assert restarted.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
