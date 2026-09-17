from decimal import Decimal
from unittest.mock import Mock

import pytest

from src.strategies.contracts import Decision, SignalType
from src.trade_management.models import ProfileSnapshot, TradePhase, TradeState
from src.trade_management.profiles.base import (
    ManagementContext,
    PlanningContext,
    ProfileResult,
    TradeManagementProfile,
)
from src.trade_management.profiles.registry import (
    get_profile_definition,
    profile_names,
    validate_strategy_compatibility,
)


class PureProbeProfile(TradeManagementProfile):
    NAME = "probe"

    def plan(self, context: PlanningContext) -> ProfileResult:
        return ProfileResult(state={"signal_id": context.signal.event_id})

    def manage(self, context: ManagementContext) -> ProfileResult:
        return ProfileResult(state={"revision": context.state.state_revision})


def test_registry_knows_all_profile_names_and_capabilities():
    assert profile_names() == ("atr_trend", "levels_rr", "ma_cloud", "pattern_targets")
    assert get_profile_definition("pattern_targets").required_strategy_capabilities == {
        "pattern_context"
    }
    assert get_profile_definition("levels_rr").required_strategy_capabilities == set()


def test_unknown_profile_is_rejected_with_available_names():
    with pytest.raises(ValueError, match="levels_rr"):
        get_profile_definition("does_not_exist")


def test_profile_rejects_strategy_missing_required_capability():
    with pytest.raises(ValueError, match="pattern_context"):
        validate_strategy_compatibility("pattern_targets", ())


def test_profile_accepts_strategy_with_required_capability():
    definition = validate_strategy_compatibility(
        "pattern_targets", {"pattern_context"}
    )

    assert definition.name == "pattern_targets"


def test_profile_contract_returns_intentions_without_broker_side_effect():
    broker = Mock()
    profile = PureProbeProfile()
    snapshot = ProfileSnapshot("levels_rr", "1", {})
    planning = PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="instrument-1",
        signal=Decision(SignalType.BUY, 100.0, event_id="signal-1"),
        profile=snapshot,
        market={},
    )
    state = TradeState(
        trade_id="trade-1",
        phase=TradePhase.OPEN,
        state_revision=2,
        quantity=1,
        average_price=Decimal("100"),
    )

    assert profile.plan(planning).state == {"signal_id": "signal-1"}
    assert profile.manage(
        ManagementContext(plan=Mock(), state=state, market={})
    ).state == {"revision": 2}
    broker.assert_not_called()
