from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.actions import AddToTrade, MoveStop, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeBroker(BrokerPort):
    def __init__(self) -> None:
        self.actions = []
        self.plans = []

    def register_trade(self, plan: TradePlan) -> None:
        self.plans.append(plan)

    def submit(self, action, now):
        self.actions.append(action)
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


def _plan() -> TradePlan:
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW,
    )


def test_manager_persists_plan_before_dispatches_and_reduces_fill(tmp_path):
    broker = FakeBroker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("1000"))
        assert manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2))
        assert storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

        events = manager.dispatch(NOW)

        assert events[0].status is ExecutionStatus.FILL
        recovered, = manager.restore()
        assert recovered.state.quantity == 2
        assert recovered.state.average_price == Decimal("100")
        assert broker.plans[-1].profile.parameters["buffer"] == "1"


def test_manager_rejects_wrong_owner_stale_revision_and_invalid_phase(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))

        with pytest.raises(ValueError, match="belong"):
            manager.submit_action(AddToTrade("add-1", "trade-1", 0, "add", 1), assignment_id="other")
        with pytest.raises(ValueError, match="invalid in phase"):
            manager.submit_action(AddToTrade("add-1", "trade-1", 0, "add", 1), assignment_id="assignment-1")

        manager.dispatch(NOW)
        with pytest.raises(ValueError, match="stale"):
            manager.submit_action(AddToTrade("add-2", "trade-1", 0, "add", 1))


def test_manager_restores_open_trade_without_current_configuration(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)

    broker = FakeBroker()
    with Storage(path) as storage:
        recovered, = TradeManager(storage, broker).restore()

    assert recovered.plan.assignment_id == "assignment-1"
    assert recovered.state.quantity == 1
    assert broker.plans == [recovered.plan]


def test_stop_ack_confirms_protection_and_advances_durable_revision(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)
        manager.submit_action(MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("100")))

        assert manager.consume(ExecutionEvent(
            "stop-1:ack", "stop-1", "stop-1", "trade-1", ExecutionStatus.ACK, 0, None,
            Decimal("0"), NOW, "confirmed",
        ))
        recovered, = manager.restore()

    assert recovered.state.confirmed_stop == Decimal("100")
    assert recovered.state.pending_stop is None
    assert recovered.state.state_revision == 2
