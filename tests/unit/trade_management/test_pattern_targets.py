from decimal import Decimal

import pandas as pd
import pytest

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import AddToTrade, MoveStop
from src.trade_management.models import ProfileSnapshot, TradePhase, TradeState
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile


def _context(side=SignalType.BUY, *, entry="106", references=None, available_at="2024-01-01T10:00:00", parameters=None):
    return PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NG-10.26",
        signal=Decision(side, float(entry), event_id="signal-1", available_at=pd.Timestamp(available_at), idea_references=references if references is not None else {
            "pattern_id": "pattern-1", "c": "104", "d": "114", "time_available": "2024-01-01T10:00:00",
        }),
        profile=ProfileSnapshot("pattern_targets", "1", parameters or {"buffer_ticks": 1}),
        market={"price_step": "1"},
    )


@pytest.mark.parametrize(("side", "entry", "references", "stop", "targets"), [
    (SignalType.BUY, "106", {"pattern_id": "long", "c": "104", "d": "114", "time_available": "2024-01-01T10:00:00"}, "103", ("110", "114")),
    (SignalType.SELL, "108", {"pattern_id": "short", "c": "110", "d": "100", "time_available": "2024-01-01T10:00:00"}, "111", ("104", "100")),
])
def test_pattern_targets_plans_confirmed_long_and_mirrored_short(side, entry, references, stop, targets):
    plan = PatternTargetsProfile().plan(_context(side, entry=entry, references=references))

    assert plan.stop_price == Decimal(stop)
    assert tuple(target.price for target in plan.targets) == tuple(map(Decimal, targets))


@pytest.mark.parametrize(("references", "available_at", "entry", "reason"), [
    ({}, "2024-01-01T10:00:00", "106", "missing-pattern-context"),
    ({"pattern_id": "p", "c": "104", "d": "114", "time_available": "2024-01-01T11:00:00"}, "2024-01-01T10:00:00", "106", "pattern-unavailable"),
    ({"pattern_id": "p", "c": "104", "d": "106", "time_available": "2024-01-01T10:00:00"}, "2024-01-01T10:00:00", "106", "target-not-ahead"),
])
def test_pattern_targets_rejects_missing_unavailable_or_behind_context(references, available_at, entry, reason):
    result = PatternTargetsProfile().plan(_context(references=references, available_at=available_at, entry=entry))

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": reason}


def test_pattern_targets_moves_stop_to_cost_aware_break_even_after_tp1():
    profile = PatternTargetsProfile()
    plan = profile.plan(_context())
    state = TradeState("trade-1", TradePhase.REDUCING, 4, 1, Decimal("106"), frozenset({"tp-1"}), confirmed_stop=plan.stop_price)

    result = profile.manage(ManagementContext(plan, state, {"price_step": "0.1", "step_cost": "10", "entry_cost": "1", "exit_cost": "1", "slippage_cost": "0.1"}))

    assert result.actions == (MoveStop("trade-1:break-even:4", "trade-1", 4, "cost-aware-break-even", Decimal("106.1")),)


def test_pattern_targets_adds_only_before_reduction_for_its_own_profitable_pattern():
    profile = PatternTargetsProfile()
    plan = profile.plan(_context(parameters={"buffer_ticks": 1, "max_adds": 1, "add_fraction": "0.5"}))
    state = TradeState("trade-1", TradePhase.OPEN, 3, 2, Decimal("106"))
    market = {"add_pattern_id": "pattern-1", "add_signal_price": "108"}

    allowed = profile.manage(ManagementContext(plan, state, market))
    wrong_pattern = profile.manage(ManagementContext(plan, state, {**market, "add_pattern_id": "pattern-2"}))
    reduced = profile.manage(ManagementContext(plan, TradeState("trade-1", TradePhase.REDUCING, 3, 2, Decimal("106")), market))

    assert allowed.actions == (AddToTrade("trade-1:pattern-add:3", "trade-1", 3, "pattern-profitable-same-formation", 1),)
    assert wrong_pattern.actions == ()
    assert reduced.actions == ()
