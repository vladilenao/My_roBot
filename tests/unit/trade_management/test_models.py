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


def _plan(side: str = "BUY", targets=(("tp-1", Decimal("104"), Decimal("1")),)) -> TradePlan:
    return TradePlan(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NGV6",
        side=side,
        signal_id="bar-1",
        reference_entry=Decimal("100"),
        stop_price=Decimal("96") if side == "BUY" else Decimal("104"),
        targets=tuple(TargetPlan(*target) for target in targets),
        profile=ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}),
        created_at=datetime.now(timezone.utc),
    )


def test_expected_r_sums_target_shares_over_risk():
    plan = _plan(targets=(("tp-1", Decimal("104"), Decimal("0.5")), ("tp-2", Decimal("108"), Decimal("0.5"))))

    assert plan.risk_per_unit == Decimal("4")
    assert plan.expected_r == Decimal("1.50")


def test_expected_r_for_short_sides_measures_adverse_move():
    plan = _plan(side="SELL", targets=(("tp-1", Decimal("96"), Decimal("1")),))

    assert plan.risk_per_unit == Decimal("4")
    assert plan.expected_r == Decimal("1.00")


def test_expected_r_without_targets_is_zero():
    assert _plan(targets=()).expected_r == Decimal("0")


def test_trade_state_rejects_average_price_without_quantity():
    with pytest.raises(ValueError, match="average_price"):
        TradeState("trade-1", average_price=Decimal("100"))


def test_addressed_action_requires_command_trade_and_revision():
    action = OpenTrade("command-1", "trade-1", 3, "entry", 2)

    assert action.command_id == "command-1"
    assert action.trade_id == "trade-1"
    assert action.state_revision == 3
