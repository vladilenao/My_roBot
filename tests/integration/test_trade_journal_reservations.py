from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier, Thread

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import ReservationCandidate, Storage


def _seed_order(storage: Storage, suffix: str, quantity: int = 2) -> None:
    now = "2026-01-01T00:00:00+00:00"
    storage.connection.execute(
        "INSERT INTO trades VALUES (?, ?, 'NGV6', ?, 'BUY', '{}', '{}', 'OPEN', 0, '{}', ?, ?)",
        (f"trade-{suffix}", f"assignment-{suffix}", f"signal-{suffix}", now, now),
    )
    storage.connection.execute(
        "INSERT INTO positions VALUES (?, 'BUY', 0, NULL, '0', '0', '0', ?)", (f"trade-{suffix}", now)
    )
    storage.connection.execute(
        "INSERT INTO outbox VALUES (?, ?, '{}', 'SENT', ?, NULL)", (f"command-{suffix}", f"trade-{suffix}", now)
    )
    storage.connection.execute(
        "INSERT INTO orders VALUES (?, ?, ?, 'OPEN', 'PENDING', ?, 0, NULL, ?, ?)",
        (f"order-{suffix}", f"trade-{suffix}", f"command-{suffix}", quantity, now, now),
    )


def _candidate(suffix: str, *, priority: int, risk: str = "800") -> ReservationCandidate:
    return ReservationCandidate(
        reservation_id=f"reservation-{suffix}", trade_id=f"trade-{suffix}", order_id=f"order-{suffix}",
        priority=priority, assignment_id=f"assignment-{suffix}", instrument_id="NGV6",
        signal_id=f"signal-{suffix}", risk_amount=Decimal(risk), margin_amount=Decimal("100"),
    )


def test_competing_candidates_are_sorted_and_reserved_in_one_sqlite_transaction(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed_order(storage, "low")
        _seed_order(storage, "high")
        storage.connection.commit()

        decisions = storage.reserve_candidates(
            (_candidate("low", priority=1), _candidate("high", priority=2)),
            risk_budget=Decimal("1000"), margin_budget=Decimal("1000"),
        )

        assert [(decision.candidate.trade_id, decision.accepted) for decision in decisions] == [
            ("trade-high", True), ("trade-low", False),
        ]
        assert storage.connection.execute(
            "SELECT trade_id, risk_amount, margin_amount FROM reservations"
        ).fetchall() == [("trade-high", "800", "100")]


def test_concurrent_reservation_attempts_cannot_spend_budget_twice(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        _seed_order(storage, "one")
        _seed_order(storage, "two")
        storage.connection.commit()

    barrier = Barrier(2)
    results: list[bool] = []

    def reserve(suffix: str) -> None:
        with Storage(path) as storage:
            barrier.wait()
            results.append(storage.reserve_candidates(
                (_candidate(suffix, priority=0),),
                risk_budget=Decimal("1000"), margin_budget=Decimal("1000"),
            )[0].accepted)

    threads = (Thread(target=reserve, args=("one",)), Thread(target=reserve, args=("two",)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with Storage(path) as storage:
        assert sorted(results) == [False, True]
        assert storage.connection.execute("SELECT SUM(risk_amount) FROM reservations").fetchone() == (800,)


def test_partial_fill_transfers_reservation_and_confirmed_cancel_releases_only_remainder(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _seed_order(storage, "one", quantity=3)
        storage.connection.execute("INSERT INTO account VALUES (1, '1000', '1000', '0', '0', '0', '2026-01-01T00:00:00+00:00')")
        storage.connection.commit()
        storage.reserve_candidates((_candidate("one", priority=0, risk="30"),), risk_budget=Decimal("100"), margin_budget=Decimal("100"))
        reducer = ExecutionReducer(storage)

        for execution_id in ("fill-1", "fill-2"):
            assert reducer.apply(ExecutionEvent(
                execution_id, "order-one", "command-one", "trade-one", ExecutionStatus.PARTIAL, 1,
                Decimal("100"), Decimal("0"), datetime.now(timezone.utc), "partial-fill",
            ))

        reservation = storage.connection.execute(
            "SELECT risk_amount, margin_amount, status FROM reservations"
        ).fetchone()
        assert tuple(map(Decimal, reservation[:2])) == (Decimal("10"), Decimal("100") / Decimal("3"))
        assert reservation[2] == "ACTIVE"
        assert reducer.apply(ExecutionEvent(
            "cancel-1", "order-one", "command-one", "trade-one", ExecutionStatus.CANCEL, 0,
            None, Decimal("0"), datetime.now(timezone.utc), "confirmed-cancel",
        ))
        assert storage.connection.execute(
            "SELECT risk_amount, margin_amount, status FROM reservations"
        ).fetchone() == ("0", "0", "RELEASED")
        assert storage.connection.execute("SELECT quantity FROM positions WHERE trade_id='trade-one'").fetchone() == (2,)
