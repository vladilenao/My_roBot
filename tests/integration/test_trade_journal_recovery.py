from decimal import Decimal

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.models import TradePhase


def test_recovery_uses_sqlite_snapshot_for_removed_assignment_and_ignores_legacy_csv(tmp_path):
    database = tmp_path / "trades.sqlite3"
    # These files are deliberately malformed legacy state. Recovery must not read them.
    (tmp_path / "journal.csv").write_text("legacy,csv,state\n", encoding="utf-8")
    (tmp_path / "positions.csv").write_text("not,a,sqlite,snapshot\n", encoding="utf-8")
    now = "2026-01-01T00:00:00+00:00"

    with Storage(database) as storage:
        connection = storage.connection
        connection.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, 'BUY', ?, ?, 'REDUCING', 7, ?, ?, ?)",
            (
                "trade-removed-assignment", "removed-assignment", "NGV6", "signal-1",
                '{"reference_entry": "100", "stop_price": "96", "targets": ['
                '{"target_id": "tp-1", "share": "0.5"}, '
                '{"target_id": "tp-2", "share": "0.5"}]}',
                '{"name": "atr_trend", "version": "1", "parameters": {'
                '"trail_k": "2", "nested": {"window": [14]}}}',
                '{"add_count": 2, "trailing_extreme": "108.5"}', now, now,
            ),
        )
        connection.execute(
            "INSERT INTO positions VALUES (?, 'BUY', 2, '101.25', '0', '0', '0', ?)",
            ("trade-removed-assignment", now),
        )
        connection.execute(
            "INSERT INTO targets VALUES ('tp-1', ?, 0, '104', 1, 1, 'FILLED')",
            ("trade-removed-assignment",),
        )
        connection.execute(
            "INSERT INTO targets VALUES ('tp-2', ?, 1, '108', 2, 0, 'PENDING')",
            ("trade-removed-assignment",),
        )
        connection.execute(
            "INSERT INTO protection VALUES (?, '102', '103', NULL, NULL, ?)",
            ("trade-removed-assignment", now),
        )
        connection.commit()

    # No active assignments are supplied: only the durable assignment ID is used.
    with Storage(database) as restarted:
        recovered, = restarted.load_trades()

    assert recovered.plan.assignment_id == "removed-assignment"
    assert recovered.plan.profile.name == "atr_trend"
    assert recovered.plan.profile.parameters["nested"]["window"] == (14,)
    with pytest.raises(TypeError):
        recovered.plan.profile.parameters["trail_k"] = "3"
    assert [(target.target_id, target.price) for target in recovered.plan.targets] == [
        ("tp-1", Decimal("104")), ("tp-2", Decimal("108")),
    ]
    assert recovered.state.phase is TradePhase.REDUCING
    assert recovered.state.state_revision == 7
    assert recovered.state.quantity == 2
    assert recovered.state.average_price == Decimal("101.25")
    assert recovered.state.completed_target_ids == {"tp-1"}
    assert recovered.state.add_count == 2
    assert recovered.state.trailing_extreme == Decimal("108.5")
    assert recovered.state.confirmed_stop == Decimal("102")
    assert recovered.state.pending_stop == Decimal("103")
