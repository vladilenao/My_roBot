from decimal import Decimal

import pytest

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import MoveStop
from src.trade_management.models import ProfileSnapshot, TradePhase, TradeState
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult
from src.trade_management.profiles.levels_rr import LevelsRrProfile


def _context(side: SignalType, market: dict[str, object]) -> PlanningContext:
    return PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NG-10.26",
        signal=Decision(side, 100.0, event_id="signal-1"),
        profile=ProfileSnapshot(
            "levels_rr",
            "1",
            {
                "buffer_ticks": 1,
                "target_R": (1, 2),
                "shares": ("0.5", "0.5"),
            },
        ),
        market={"price_step": "1", **market},
    )


@pytest.mark.parametrize(("side", "market", "stop", "targets"), [
    (SignalType.BUY, {"support": "97"}, "96", ("104", "108")),
    (SignalType.SELL, {"resistance": "103"}, "104", ("96", "92")),
])
def test_levels_rr_plans_correct_side_structure_and_r_targets(side, market, stop, targets):
    plan = LevelsRrProfile().plan(_context(side, market))

    assert plan.stop_price == Decimal(stop)
    assert tuple(target.price for target in plan.targets) == tuple(map(Decimal, targets))


@pytest.mark.parametrize(("side", "market"), [
    (SignalType.BUY, {}),
    (SignalType.BUY, {"support": "101"}),
    (SignalType.SELL, {}),
    (SignalType.SELL, {"resistance": "99"}),
])
def test_levels_rr_rejects_missing_or_wrong_side_structure(side, market):
    result = LevelsRrProfile().plan(_context(side, market))

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "missing-structure"}


@pytest.mark.parametrize(("side", "market", "expected"), [
    (
        SignalType.BUY,
        {
            "support": "97",
            "price_step": "0.1",
            "step_cost": "10",
            "entry_cost": "1",
            "exit_cost": "1",
            "slippage_cost": "0.1",
        },
        "100.1",
    ),
    (
        SignalType.SELL,
        {
            "resistance": "103",
            "price_step": "0.1",
            "step_cost": "10",
            "entry_cost": "1",
            "exit_cost": "1",
            "slippage_cost": "0.1",
        },
        "99.9",
    ),
])
def test_levels_rr_moves_stop_to_cost_aware_break_even_after_first_target(side, market, expected):
    profile = LevelsRrProfile()
    plan = profile.plan(_context(side, market))
    state = TradeState(
        trade_id=plan.trade_id,
        phase=TradePhase.REDUCING,
        state_revision=4,
        quantity=1,
        average_price=Decimal("100"),
        completed_target_ids=frozenset({"tp-1"}),
        confirmed_stop=plan.stop_price,
    )

    result = profile.manage(ManagementContext(plan=plan, state=state, market=market))

    assert result.actions == (
        MoveStop("trade-1:break-even:4", "trade-1", 4, "cost-aware-break-even", Decimal(expected)),
    )


def test_levels_rr_does_not_move_stop_before_first_target():
    profile = LevelsRrProfile()
    plan = profile.plan(_context(SignalType.BUY, {"support": "97"}))
    state = TradeState("trade-1", TradePhase.OPEN, 1, 1, Decimal("100"))

    result = profile.manage(
        ManagementContext(plan, state, {"price_step": "1", "step_cost": "100"})
    )

    assert result.actions == ()
