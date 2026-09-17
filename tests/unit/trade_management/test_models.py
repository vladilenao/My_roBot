from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.trade_management import (
    OpenTrade,
    ProfileSnapshot,
    TargetPlan,
    TradePlan,
    TradeState,
)


def test_trade_plan_captures_immutable_owner_and_profile_snapshot():
    plan = TradePlan(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NGV6",
        side="BUY",
        signal_id="bar-1",
        reference_entry=Decimal("100"),
        stop_price=Decimal("96"),
        targets=(TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
        profile=ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}),
        created_at=datetime.now(timezone.utc),
    )

    assert plan.assignment_id == "assignment-1"
    assert plan.profile.name == "levels_rr"


def test_trade_state_rejects_average_price_without_quantity():
    with pytest.raises(ValueError, match="average_price"):
        TradeState("trade-1", average_price=Decimal("100"))


def test_addressed_action_requires_command_trade_and_revision():
    action = OpenTrade("command-1", "trade-1", 3, "entry", 2)

    assert action.command_id == "command-1"
    assert action.trade_id == "trade-1"
    assert action.state_revision == 3
