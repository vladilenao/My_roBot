"""Снимок журнала для сценария гэпа.

Защита от гэпа держится на нескольких вещах сразу: вход исполняется не по той
цене, по которой планировался, и слишком дорогой вход отменяется; стоп и цели
переезжают на среднюю, по которой позиция реально стоит; стоп за границей рынка
не применяется вовсе. Каждая из них меняет числа в журнале, поэтому эталонный
снимок ловит их все разом — в отличие от проверок по одной, где опечатка в пути
остаётся незамеченной.

Перезаписать эталоны:

    .venv/bin/python -m pytest tests/snapshot/test_gap_journal.py --update-snapshots
"""

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker import JournalBroker
from src.portfolio import PositionManager
from src.trade_journal import TradeJournal
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop, OpenTrade
from src.trade_management.errors import TradeManagementException
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TICKER = "NG"
SNAPSHOT = "gap_journal_expected.csv"
_COLUMNS = ["stage", "field", "value"]


def _plan(trade_id: str) -> TradePlan:
    return TradePlan(
        trade_id, "assignment-1", TICKER, "BUY", f"signal-{trade_id}", Decimal("100"),
        Decimal("96"),
        (
            TargetPlan("tp-1", Decimal("104"), Decimal("1"), Decimal("4")),
            TargetPlan("tp-2", Decimal("108"), Decimal("1"), Decimal("8")),
        ),
        ProfileSnapshot("levels_rr", "1", {}), NOW,
    )


def _manager(tmp_path, name: str, tolerance: str) -> TradeManager:
    journal = TradeJournal.created_on_init(tmp_path / f"{name}.csv")
    storage = Storage(
        tmp_path / f"{name}.sqlite3",
        journal_path=tmp_path / f"{name}_out.csv",
        positions_path=tmp_path / f"{name}_positions.csv",
    )
    broker = JournalBroker(
        journal, PositionManager(initial_deposit=1_000_000, max_risk_pct=2), ["14:05"]
    )
    return TradeManager(
        storage, broker, initial_balance=Decimal("1000000"),
        slippage_tolerance=Decimal(tolerance),
    )


def _revision(manager, trade_id) -> int:
    with manager._storage.transaction() as connection:
        return connection.execute(
            "SELECT state_revision FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()[0]


def _step(manager, at, open_, low, high, close):
    """Deliver what was queued, let the broker act on the next bar, deliver that.

    Entries and stop changes activate on the next processed bar strictly after
    their acknowledgement, so the bar has to carry a later timestamp than the
    submission. The entry fills at the bar's open, which is the price the whole
    rest of the scenario is measured from.
    """
    manager.dispatch(at)
    bar_at = at + timedelta(minutes=1)
    manager._broker.track_bar(bar_at, {TICKER: (open_, low, high, close)}, {})
    for event in manager._broker.drain_addressed_events():
        manager.consume(event)
    manager.dispatch(bar_at)
    return bar_at


def _gapped_entry(tmp_path):
    """Вход исполняется выше цены сигнала: сделка отменяется, позиция закрывается."""
    manager = _manager(tmp_path, "gap", "0.005")
    manager.submit_plan(_plan("trade-gap"), OpenTrade("c-open", "trade-gap", 0, "entry", 3))
    _step(manager, NOW, 102.0, 101.5, 102.5, 102.0)
    return manager, NOW + timedelta(minutes=1)


def _rebased_trade(tmp_path):
    """Вход в пределах допуска: стоп и цели следуют за фактической средней."""
    manager = _manager(tmp_path, "rebase", "1")
    manager.submit_plan(_plan("trade-rebase"), OpenTrade("c-open", "trade-rebase", 0, "entry", 3))
    _step(manager, NOW, 100.5, 100.0, 101.0, 100.5)

    manager.submit_action(
        MoveStop("c-stop", "trade-rebase", _revision(manager, "trade-rebase"),
                 "breakeven", Decimal("100")),
        market_close=Decimal("100.5"),
    )
    _step(manager, NOW + timedelta(minutes=1), 100.5, 100.2, 101.5, 101.0)

    refused = False
    try:
        manager.submit_action(
            MoveStop("c-greedy", "trade-rebase", _revision(manager, "trade-rebase"),
                     "greedy", Decimal("110")),
            market_close=Decimal("101.0"),
        )
    except TradeManagementException:
        refused = True
    return manager, refused


def _position(manager, trade_id) -> list[tuple[str, str]]:
    with manager._storage.transaction() as connection:
        quantity, average = connection.execute(
            "SELECT quantity, average_price FROM positions WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        stop = connection.execute(
            "SELECT confirmed_stop FROM protection WHERE trade_id = ?", (trade_id,)
        ).fetchone()
    return [
        ("phase", connection.execute(
            "SELECT phase FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()[0]),
        ("quantity", str(quantity)),
        ("average_price", str(average)),
        ("confirmed_stop", str(stop[0]) if stop else "none"),
    ]


def _targets(manager, trade_id) -> list[tuple[str, str]]:
    with manager._storage.transaction() as connection:
        rows = connection.execute(
            "SELECT target_id, price FROM targets WHERE trade_id = ? ORDER BY target_id",
            (trade_id,),
        ).fetchall()
    return [(f"target:{target}", str(price)) for target, price in rows]


def _rejections(manager) -> list[tuple[str, str]]:
    with manager._storage.transaction() as connection:
        rows = connection.execute(
            "SELECT algorithm, reason FROM calculations WHERE outcome = 'REJECTED' ORDER BY algorithm"
        ).fetchall()
    return [(f"rejected:{algorithm}", reason) for algorithm, reason in rows]


def _queued(manager) -> list[tuple[str, str]]:
    with manager._storage.transaction() as connection:
        rows = connection.execute(
            "SELECT command_id, status FROM outbox ORDER BY created_at, command_id"
        ).fetchall()
    return [(f"outbox:{command}", status) for command, status in rows]


@pytest.fixture()
def snapshot(tmp_path):
    gap, _ = _gapped_entry(tmp_path)
    rebase, refused = _rebased_trade(tmp_path)
    return [
        *[("gap", field, value) for field, value in _position(gap, "trade-gap")],
        *[("gap", field, value) for field, value in _queued(gap)],
        *[("gap", field, value) for field, value in _rejections(gap)],
        *[("rebase", field, value) for field, value in _position(rebase, "trade-rebase")],
        *[("rebase", field, value) for field, value in _targets(rebase, "trade-rebase")],
        *[("rebase", field, value) for field, value in _rejections(rebase)],
        ("boundary", "stop_beyond_market_refused", str(refused)),
    ]


def test_gap_journal_snapshot(snapshot, request):
    actual = [_COLUMNS, *[list(row) for row in snapshot]]
    path = request.config.rootpath / "tests" / "snapshot" / "data" / SNAPSHOT

    if request.config.getoption("--update-snapshots"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(actual)
        return

    with path.open(newline="", encoding="utf-8") as handle:
        expected = [row for row in csv.reader(handle) if row]

    if actual == expected:
        return
    for index, (got, want) in enumerate(zip(actual, expected)):
        if got != want:
            pytest.fail(
                "Снимок журнала разошёлся с эталоном.\n"
                f"Строка {index}:\n  фактическое: {dict(zip(_COLUMNS, got, strict=True))}\n"
                f"  эталонное:   {dict(zip(_COLUMNS, want, strict=True))}\n"
                "Перезаписать: pytest tests/snapshot/test_gap_journal.py --update-snapshots"
            )
    pytest.fail(f"Длина снимка разошлась: фактическая {len(actual)}, эталонная {len(expected)}")