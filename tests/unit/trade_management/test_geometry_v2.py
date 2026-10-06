"""Границы и cost-aware цены проверяются независимыми Decimal-примерами."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.strategies.contracts import Decision, SignalType
from src.trade_management.models import ProfileSnapshot, rebase_on_average
from src.trade_management.profiles.base import PlanningContext, ProfileResult, bounded_stop_geometry
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile

D = Decimal
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
GEOMETRY = {"min_stop_atr": 0, "min_stop_ticks": 1, "stop_beyond_bar": 0, "max_stop_atr": None}


def context(side="BUY", *, parameters=None, market=None, name="levels_rr", entry=100, references=None):
    return PlanningContext(
        "trade", "assignment", "SBER",
        Decision(SignalType(side), entry, event_id="signal", available_at=NOW, idea_references=references),
        ProfileSnapshot(name, "1", {"target_R": (1, 2), "shares": ("0.5", "0.5"), **GEOMETRY, **(parameters or {})}),
        {"algorithm_version": "economics-v2", "price_step": D(1), "step_cost": D(1),
         "support": D(97), "resistance": D(103), "entry_cost": D("1.5"),
         "exit_cost": D("1.5"), "slippage_cost": D(1), "created_at": NOW, **(market or {})},
    )


@pytest.mark.parametrize("side,stop,targets", [("BUY", 96, (108, 112)), ("SELL", 104, (92, 88))])
def test_unchanged_structural_stop_still_compensates_costs(side, stop, targets):
    ctx = context(side)
    p = LevelsRrProfile().plan(ctx)
    assert p.stop_price == D(stop)
    assert tuple(t.price for t in p.targets) == tuple(map(D, targets))
    assert p.stop_basis == "structural"
    again = bounded_stop_geometry(p, ctx.market)
    assert again.targets == p.targets  # no repeated +4


@pytest.mark.parametrize("side,expected", [("BUY", 105), ("SELL", 95)])
def test_cost_compensation_rounds_away_from_entry(side, expected):
    p = LevelsRrProfile().plan(context(side, market={"step_cost": D(10)}))
    assert p.targets[0].price == D(expected)  # 4R-price + 0.4 cost-price, directed tick


@pytest.mark.parametrize("parameters,market,floor,cap", [
    ({"min_stop_ticks": 5, "max_stop_atr": 3}, {"atr": D(1)}, D(5), D(3)),
    ({"min_stop_ticks": 0, "max_stop_atr": "0.5"}, {"atr": D(1)}, D(0), D("0.5")),
    ({"stop_beyond_bar": 1, "max_stop_atr": 3}, {"atr": D(1), "bar_range": D(5)}, D(5), D(3)),
])
def test_incompatible_bounds_return_numerical_rejection(parameters, market, floor, cap):
    result, trace = LevelsRrProfile().plan_with_trace(context(parameters=parameters, market=market))
    assert isinstance(result, ProfileResult)
    assert result.state["reason"] == "stop-bounds-conflict"
    assert result.state["floor"] == floor and result.state["cap"] == cap
    assert trace.reason == "stop-bounds-conflict"
    assert trace.result.value["price_step"] == 1


def test_absent_atr_keeps_tick_and_bar_bounds():
    p = LevelsRrProfile().plan(context(parameters={"min_stop_ticks": 5, "max_stop_atr": 1}, market={"bar_range": D(2)}))
    assert p.stop_price == 95


def test_compatible_cap_allows_less_than_one_tick_of_rounding():
    p = LevelsRrProfile().plan(context("SELL", entry=D("100.1"), parameters={"max_stop_atr": "2.5"}, market={"atr": D(1)}))
    assert p.stop_price == 103
    assert D("0") < p.risk_per_unit-D("2.5") < D(1)


def test_short_nonpositive_cost_aware_target_is_a_profile_refusal():
    result = LevelsRrProfile().plan(context("SELL", market={"entry_cost": D(50), "exit_cost": D(50), "slippage_cost": D(0)}))
    assert isinstance(result, ProfileResult)
    assert result.state["reason"] == "target-not-ahead"


def test_absolute_pattern_prices_survive_costs_and_rebase():
    ctx = context(name="pattern_targets", entry=106,
                  references={"pattern_id": "abcd", "c": 104, "d": 114, "time_available": NOW})
    p = PatternTargetsProfile().plan(ctx)
    assert tuple(t.price for t in p.targets) == (D(110), D(114))
    assert tuple(t.price for t in rebase_on_average(p, D("105.5")).targets) == (D(110), D(114))


def test_rebase_uses_saved_grid_without_cost_accumulation():
    p = LevelsRrProfile().plan(context())
    shifted = rebase_on_average(p, D("99.7"))
    assert shifted.stop_price == 95
    assert tuple(t.price for t in shifted.targets) == (D(108), D(112))
    assert rebase_on_average(shifted, D("99.7")) == shifted
    assert p.reference_entry == 100 and p.stop_price == 96
    legacy = replace(p, algorithm_version="legacy-v1", entry_order_type="market", cost_snapshot=None)
    assert rebase_on_average(legacy, D("99.7")).stop_price == D("95.7")
