"""Адресность записей целей: состояние цели принадлежит своей сделке.

Регрессия на ``reducer._update_target``: ``UPDATE targets WHERE target_id=?``
без ``trade_id`` матчил одноимённые цели всех сделок, потому что первичный
ключ таблицы — составной ``(trade_id, target_id)``, а обозначение цели
уникально только внутри сделки.
"""

from datetime import datetime, timezone
from decimal import Decimal

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

GRID = (
    TargetPlan("tp-1", Decimal("104"), Decimal("1")),
    TargetPlan("tp-2", Decimal("108"), Decimal("1")),
)


class FillingBroker(BrokerPort):
    """Брокер, исполняющий вход по цене плана и не трогающий защиту."""

    def __init__(self) -> None:
        self.plans: list[TradePlan] = []

    def register_trade(self, plan: TradePlan) -> None:
        self.plans.append(plan)

    def submit(self, action, now):
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


def _plan(trade_id: str, ticker: str, signal_id: str) -> TradePlan:
    return TradePlan(
        trade_id, "assignment-1", ticker, "BUY", signal_id, Decimal("100"), Decimal("96"),
        GRID, ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW,
    )


def _target_fill(trade_id: str, target_id: str, quantity: int) -> ExecutionEvent:
    command_id = f"{trade_id}:pv:tp:{target_id}:{LATER.isoformat()}"
    return ExecutionEvent(
        execution_id=f"{command_id}:fill", order_id=command_id, command_id=command_id,
        trade_id=trade_id, status=ExecutionStatus.FILL, filled_quantity=quantity,
        price=Decimal("104"), fee=Decimal("0"), timestamp=LATER, reason=f"tp:{target_id}",
    )


def _open_two_trades(storage: Storage) -> TradeManager:
    """Две сделки на разных тикерах с одинаковой сеткой целей."""
    manager = TradeManager(storage, FillingBroker(), initial_balance=Decimal("100000"))
    assert manager.submit_plan(_plan("trade-a", "NGV6", "signal-a"), OpenTrade("open-a", "trade-a", 0, "entry", 2))
    assert manager.submit_plan(_plan("trade-b", "NGU6", "signal-b"), OpenTrade("open-b", "trade-b", 0, "entry", 2))
    manager.dispatch(NOW)
    return manager


def test_target_fill_of_one_trade_keeps_parallel_trade_targets_pending(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _open_two_trades(storage)

        assert ExecutionReducer(storage).apply(_target_fill("trade-a", "tp-1", 2)) is True

        recovered = {trade.plan.trade_id: trade for trade in storage.load_trades(include_terminal=True)}

        assert recovered["trade-a"].state.completed_target_ids == frozenset({"tp-1"})
        assert recovered["trade-b"].state.completed_target_ids == frozenset()


def test_target_fill_does_not_touch_named_target_of_parallel_trade(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _open_two_trades(storage)

        ExecutionReducer(storage).apply(_target_fill("trade-a", "tp-1", 2))

        rows = storage.connection.execute(
            "SELECT trade_id, target_id, filled_quantity, status FROM targets "
            "WHERE target_id = 'tp-1' ORDER BY trade_id"
        ).fetchall()

        assert rows == [("trade-a", "tp-1", 2, "FILLED"), ("trade-b", "tp-1", 0, "PENDING")]


def test_parallel_trade_reports_no_filled_volume_for_foreign_target(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _open_two_trades(storage)

        ExecutionReducer(storage).apply(_target_fill("trade-a", "tp-1", 2))

        recovered = {trade.plan.trade_id: trade for trade in storage.load_trades(include_terminal=True)}

        assert recovered["trade-a"].target_filled["tp-1"] == 2
        assert recovered["trade-b"].target_filled["tp-1"] == 0


def test_partial_target_fill_is_addressed_as_well(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _open_two_trades(storage)

        assert ExecutionReducer(storage).apply(_target_fill("trade-a", "tp-1", 1)) is True

        rows = storage.connection.execute(
            "SELECT trade_id, filled_quantity, status FROM targets "
            "WHERE target_id = 'tp-1' ORDER BY trade_id"
        ).fetchall()

        assert rows == [("trade-a", 1, "PARTIAL"), ("trade-b", 0, "PENDING")]