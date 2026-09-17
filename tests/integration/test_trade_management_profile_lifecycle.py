from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, MoveStop, OpenTrade, ReduceTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.base import ManagementContext
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.ma_cloud import MaCloudProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile


NOW = datetime(2026, 1, 1, tzinfo=UTC)


class SimulatedBroker(BrokerPort):
    """Synchronous deterministic executor used only by this SQLite integration test."""

    def submit(self, action, now):
        if isinstance(action, MoveStop):
            return ExecutionEvent(
                f"{action.command_id}:ack", action.command_id, action.command_id, action.trade_id,
                ExecutionStatus.ACK, 0, None, Decimal("0"), now, action.reason,
            )
        quantity = 2 if isinstance(action, CloseTrade) else action.quantity
        price = Decimal("100") if isinstance(action, OpenTrade) else Decimal("102")
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, quantity,
            price, Decimal("0"), now, action.reason,
        )


def _plan(name, parameters, targets):
    return TradePlan(
        f"trade-{name}", f"assignment-{name}", "NGV6", "BUY", f"signal-{name}",
        Decimal("100"), Decimal("96"), targets, ProfileSnapshot(name, "1", parameters), NOW,
    )


def _state(manager):
    recovered, = manager.restore()
    return recovered


@pytest.mark.parametrize("name", ["levels_rr", "atr_trend", "ma_cloud", "pattern_targets"])
def test_profiles_complete_entry_add_partial_stop_restart_and_close_in_sqlite(tmp_path, name):
    profile, plan, add_market, stop_market, partial = _scenario(name)
    database = tmp_path / f"{name}.sqlite3"

    with Storage(database) as storage:
        manager = TradeManager(storage, SimulatedBroker(), initial_balance=Decimal("10000"))
        manager.submit_plan(plan, OpenTrade(f"{name}:open", plan.trade_id, 0, "entry", 2))
        manager.dispatch(NOW)

        recovered = _state(manager)
        add = next(action for action in profile.manage(ManagementContext(plan, recovered.state, add_market)).actions
                   if action.__class__.__name__ == "AddToTrade")
        manager.submit_action(add)
        manager.dispatch(NOW)

        # Target allocation follows the actual entry and add fills, then one contract exits.
        with storage.transaction() as connection:
            connection.execute("UPDATE targets SET planned_quantity=1 WHERE trade_id=? AND target_id='tp-1'", (plan.trade_id,))
        recovered = _state(manager)
        if partial is not None:
            intended = next(action for action in profile.manage(
                ManagementContext(plan, recovered.state, partial)
            ).actions if isinstance(action, ReduceTrade))
            partial_action = ReduceTrade(
                intended.command_id, intended.trade_id, intended.state_revision, intended.reason,
                intended.quantity, "tp-1",
            )
        else:
            partial_action = ReduceTrade(
                f"{name}:partial", plan.trade_id, recovered.state.state_revision,
                "profile-partial", 1, "tp-1",
            )
        manager.submit_action(partial_action)
        manager.dispatch(NOW)

        recovered = _state(manager)
        move = next(action for action in profile.manage(ManagementContext(plan, recovered.state, stop_market)).actions
                    if isinstance(action, MoveStop))
        manager.submit_action(move)
        manager.dispatch(NOW)

    # Recovery is exclusively from the temporary SQLite database.
    with Storage(database) as storage:
        manager = TradeManager(storage, SimulatedBroker())
        recovered = _state(manager)
        assert recovered.state.quantity == 2
        assert recovered.state.confirmed_stop is not None
        assert recovered.state.completed_target_ids == {"tp-1"}

        manager.submit_action(CloseTrade(f"{name}:close", plan.trade_id, recovered.state.state_revision, "scenario-close"))
        manager.dispatch(NOW)
        assert storage.connection.execute("SELECT phase FROM trades WHERE trade_id=?", (plan.trade_id,)).fetchone()[0] == "CLOSED"


def _scenario(name):
    target = (TargetPlan("tp-1", Decimal("104"), Decimal("0.5")),)
    if name == "levels_rr":
        profile = LevelsRrProfile()
        return profile, _plan(name, {"max_adds": 1, "add_fraction": "0.5"}, target), {"add_signal_price": "102"}, {
            "price_step": "1", "step_cost": "100",
        }, None
    if name == "atr_trend":
        profile = AtrTrendProfile()
        return profile, _plan(name, {"initial_k": "2", "trail_k": "2", "max_adds": 1,
                                     "add_fraction": "0.5", "advance_R": "0.5"}, target), {
            "add_signal_price": "102", "last_entry_price": "100",
        }, {"price_step": "1", "atr": "2", "high": "106"}, None
    if name == "ma_cloud":
        profile = MaCloudProfile()
        return profile, _plan(name, {"max_adds": 1, "add_fraction": "0.5"}, target), {
            "price_step": "1", "ma10": "100", "ma40": "99", "close": "107",
            "cloud_retest": True, "add_signal_price": "106", "bar_id": "add",
        }, {"price_step": "1", "ma10": "108", "ma40": "104", "close": "109", "bar_id": "stop"}, {
            "price_step": "1", "ma10": "107", "ma40": "103", "close": "105", "bar_id": "partial",
        }
    profile = PatternTargetsProfile()
    return profile, _plan(name, {"max_adds": 1, "add_fraction": "0.5", "_pattern_id": "p-1"}, target), {
        "add_pattern_id": "p-1", "add_signal_price": "108",
    }, {"price_step": "1", "step_cost": "100"}, None


@pytest.mark.parametrize("profile", [LevelsRrProfile(), PatternTargetsProfile()])
def test_default_disabled_adds_reject_but_explicit_configuration_permits(profile):
    name = profile.NAME
    target = (TargetPlan("tp-1", Decimal("104"), Decimal("0.5")),)
    parameters = {"_pattern_id": "p-1"} if name == "pattern_targets" else {}
    plan = _plan(name, parameters, target)
    state = _open_state(plan.trade_id)
    market = {"add_signal_price": "102"}
    if name == "pattern_targets":
        market["add_pattern_id"] = "p-1"

    assert not profile.manage(ManagementContext(plan, state, market)).actions

    enabled = _plan(name, {**parameters, "max_adds": 1, "add_fraction": "0.5"}, target)
    assert any(action.__class__.__name__ == "AddToTrade"
               for action in profile.manage(ManagementContext(enabled, state, market)).actions)


def _open_state(trade_id):
    from src.trade_management.models import TradePhase, TradeState

    return TradeState(trade_id, TradePhase.OPEN, 1, 2, Decimal("100"))
