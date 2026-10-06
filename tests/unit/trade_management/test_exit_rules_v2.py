"""BE всей сделки и причинная активация трейлинга новой версии."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import MoveStop
from src.trade_management.models import CostSnapshot, ProfileSnapshot, TargetPlan, TradePhase, TradePlan, TradeState
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.base import ManagementContext, PlanningContext
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile
from src.trade_management.profiles.rules import whole_trade_break_even

D = Decimal
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("side,gross,fees,future,expected", [
    ("BUY", 20, "22.5", 10, "102.5"), ("SELL", 20, "22.5", 10, "97.5"),
    ("BUY", 50, 10, 5, 93), ("SELL", 50, 10, 5, 107),
])
def test_whole_trade_be_independent_cash_examples(side, gross, fees, future, expected):
    assert whole_trade_break_even(D(100), side, D("0.1"), D("0.1"), 5,
        realized_gross=D(gross), paid_fees=D(fees), expected_exit_cost=D(future)) == D(expected)


def be_context(name="levels_rr", **state_changes):
    p = TradePlan("trade", "assignment", "SBER", "BUY", "signal", D(100), D(96),
        (TargetPlan("tp-1", D(108), D("0.5")), TargetPlan("tp-2", D(112), D("0.5"))),
        ProfileSnapshot(name, "1", {"min_be_r": D("1.5")}), NOW,
        algorithm_version="economics-v2", cost_snapshot=CostSnapshot(D("1.5"), D(1)), entry_order_type="limit")
    values = dict(phase=TradePhase.REDUCING, quantity=5, average_price=D(100),
                  completed_target_ids=frozenset({"tp-1"}), confirmed_stop=D(96),
                  initial_stop_distance=D(4), fees=D("22.5"), realized_pnl=D(20), fees_known=True)
    values.update(state_changes)
    return ManagementContext(p, TradeState("trade", **values),
                             {"price_step": D("0.1"), "step_cost": D("0.1"), "close": D(106)})


@pytest.mark.parametrize("name,profile", [("levels_rr", LevelsRrProfile), ("pattern_targets", PatternTargetsProfile)])
def test_tp1_and_current_1_5_d0_are_both_required(name, profile):
    ctx = be_context(name)
    below = replace(ctx, market={**ctx.market, "close": D(104)})
    result = profile().manage(below)
    assert not result.actions and result.state["be_skip_reason"] == "below-min-be-r"
    result = profile().manage(ctx)
    assert len(result.actions) == 1
    assert isinstance(result.actions[0], MoveStop) and result.actions[0].stop_price == D("102.5")


@pytest.mark.parametrize("changes,market,reason", [
    ({"fees_known": False}, {}, "costs-unknown"),
    ({"initial_stop_distance": None}, {}, "initial-risk-or-protection-unknown"),
    ({}, {"close": None}, "market-unknown"),
    ({"confirmed_stop": D(103)}, {}, "protection-not-improved"),
])
def test_unknown_inputs_or_no_improvement_skip_with_audit_reason(changes, market, reason):
    ctx = be_context(**changes)
    result = LevelsRrProfile().manage(replace(ctx, market={**ctx.market, **market}))
    assert not result.actions and result.state["be_skip_reason"] == reason


def test_profit_can_put_whole_trade_be_below_average():
    ctx = be_context(realized_pnl=D(50), fees=D(10), confirmed_stop=D(90))
    ctx = replace(ctx, plan=replace(ctx.plan, cost_snapshot=CostSnapshot(D(1), D(0))))
    assert LevelsRrProfile().manage(ctx).actions[0].stop_price == D(93)


def test_new_be_threshold_does_not_apply_to_legacy_plan():
    ctx = be_context(fees_known=False)
    ctx = replace(ctx, plan=replace(ctx.plan, algorithm_version="legacy-v1", entry_order_type="market", cost_snapshot=None),
                  market={**ctx.market, "close": D(104), "entry_cost": D(1), "exit_cost": D(1), "slippage_cost": D(1)})
    assert LevelsRrProfile().manage(ctx).actions[0].stop_price == D("100.6")


def trend_context(quantity=3):
    parameters = {"target_R": (1, 2), "shares": ("0.25", "0.25"), "min_stop_ticks": 1,
                  "min_stop_atr": 0, "stop_beyond_bar": 0, "max_stop_atr": None, "max_adds": 2}
    market = {"algorithm_version": "economics-v2", "price_step": D(1), "step_cost": D(1),
              "atr": D(2), "entry_cost": D("1.5"), "exit_cost": D("1.5"), "slippage_cost": D(1)}
    p = AtrTrendProfile().plan(PlanningContext("trend", "assignment", "SBER",
        Decision(SignalType.BUY, 100, event_id="signal"), ProfileSnapshot("atr_trend", "1", parameters), market))
    s = TradeState("trend", phase=TradePhase.OPEN, quantity=quantity, max_quantity=quantity,
                   average_price=D(100), confirmed_stop=D(96), initial_stop_distance=D(4))
    return ManagementContext(p, s, {**market, "high": D(108), "low": D(100), "close": D(107), "holding_bar_eligible": True})


@pytest.mark.parametrize("quantity", [1, 3])
def test_zero_first_allocation_activates_on_cost_aware_threshold_without_fake_fill(quantity):
    ctx = trend_context(quantity)
    assert tuple(t.price for t in ctx.plan.targets) == (D(108), D(112))
    profile = AtrTrendProfile()
    below = profile.manage(replace(ctx, market={**ctx.market, "high": D(107)}))
    assert not below.actions and not below.state
    intrabar = profile.manage(replace(ctx, market={**ctx.market, "holding_bar_eligible": False}))
    assert not intrabar.actions and not intrabar.state
    result = profile.manage(ctx)
    assert result.state["trailing_active"] and result.state["adds_disabled"]
    assert result.state["trailing_extreme"] == D(108)
    assert result.actions[0].stop_price == D(104)
    assert not ctx.state.completed_target_ids


def test_nonzero_first_allocation_requires_a_confirmed_reduction():
    ctx = trend_context(8)
    assert not AtrTrendProfile().manage(ctx).state
    confirmed = replace(ctx, state=replace(ctx.state, completed_target_ids=frozenset({"tp-1"}), quantity=6))
    assert AtrTrendProfile().manage(confirmed).state["trailing_active"]


def test_restored_trailing_stays_active_after_price_falls_below_threshold():
    ctx = trend_context()
    restored = replace(ctx, state=replace(ctx.state, trailing_active=True, adds_disabled=True, trailing_extreme=D(108)),
                       market={**ctx.market, "high": D(106), "close": D(105)})
    result = AtrTrendProfile().manage(restored)
    assert result.state["trailing_active"] and result.state["trailing_extreme"] == D(108)
