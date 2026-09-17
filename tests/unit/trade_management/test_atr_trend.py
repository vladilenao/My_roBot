from decimal import Decimal

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import AddToTrade, MoveStop
from src.trade_management.models import ProfileSnapshot, TradePhase, TradeState
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult
from src.trade_management.profiles.rules import TargetAllocation, allocate_target_quantities


def _planning_context(side=SignalType.BUY, *, atr="2"):
    return PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NG-10.26",
        signal=Decision(side, 100.0, event_id="signal-1"),
        profile=ProfileSnapshot(
            "atr_trend",
            "1",
            {"initial_k": "2", "trail_k": "2", "tp1_R": "1", "tp1_share": "0.5", "max_adds": 2, "add_fraction": "0.5", "advance_R": "0.5"},
        ),
        market={"price_step": "0.125", "atr": atr},
    )


def _state(plan, revision, *, quantity=1, completed=False, stop=None, extreme=None, adds=0):
    return TradeState(
        trade_id=plan.trade_id,
        phase=TradePhase.REDUCING if completed else TradePhase.OPEN,
        state_revision=revision,
        quantity=quantity,
        average_price=Decimal("100"),
        completed_target_ids=frozenset({"tp-1"}) if completed else frozenset(),
        confirmed_stop=Decimal(stop) if stop else plan.stop_price,
        trailing_extreme=Decimal(extreme) if extreme else None,
        add_count=adds,
    )


def test_atr_trend_plans_initial_atr_stop_and_tp1():
    plan = AtrTrendProfile().plan(_planning_context())

    assert plan.stop_price == Decimal("96")
    assert plan.targets[0].price == Decimal("104")
    assert plan.targets[0].share == Decimal("0.5")


def test_atr_trend_allocates_tp1_from_actual_quantity_and_keeps_q1_trailing():
    plan = AtrTrendProfile().plan(_planning_context())

    two_contracts = allocate_target_quantities(
        2, plan.targets, retain_remainder_for_trailing=True
    )
    one_contract = allocate_target_quantities(
        1, plan.targets, retain_remainder_for_trailing=True
    )

    assert two_contracts.targets == (TargetAllocation("tp-1", 1),)
    assert two_contracts.trailing_quantity == 1
    assert one_contract.targets == ()
    assert one_contract.trailing_quantity == 1


def test_atr_trend_rejects_entry_without_warmed_atr():
    result = AtrTrendProfile().plan(_planning_context(atr=None))

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "insufficient-history"}


def test_atr_trend_trails_monotonically_through_documented_atr_sequence():
    profile = AtrTrendProfile()
    plan = profile.plan(_planning_context())
    cases = (
        ("2.5", "103", "96", None),
        ("2.75", "105", "96", "99.5"),
        ("2.875", "107", "99.5", "101.25"),
        ("2.9375", "109", "101.25", "103.125"),
    )
    extreme = None
    for revision, (atr, high, stop, expected) in enumerate(cases, start=1):
        state = _state(plan, revision, completed=revision > 1, stop=stop, extreme=extreme)
        result = profile.manage(ManagementContext(plan, state, {"price_step": "0.125", "atr": atr, "high": high}))
        extreme = high
        if expected is None:
            assert result.actions == ()
        else:
            assert result.actions == (MoveStop(f"trade-1:atr-trail:{revision}", "trade-1", revision, "atr-trailing-stop", Decimal(expected)),)

    lower = profile.manage(ManagementContext(plan, _state(plan, 5, completed=True, stop="103.125", extreme="109"), {"price_step": "0.125", "atr": "4.46875", "high": "108"}))
    assert lower.actions == ()
    assert lower.state["trailing_extreme"] == Decimal("109")


def test_atr_trend_q1_activates_trailing_without_a_zero_tp_order():
    profile = AtrTrendProfile()
    plan = profile.plan(_planning_context())
    result = profile.manage(ManagementContext(plan, _state(plan, 3), {"price_step": "0.125", "atr": "2.75", "high": "105"}))

    assert result.actions == (MoveStop("trade-1:atr-trail:3", "trade-1", 3, "atr-trailing-stop", Decimal("99.5")),)
    assert result.state == {"trailing_active": True, "trailing_extreme": Decimal("105"), "adds_disabled": True}


def test_atr_trend_only_proposes_profitable_bounded_pyramiding_inputs():
    profile = AtrTrendProfile()
    plan = profile.plan(_planning_context())
    allowed = profile.manage(ManagementContext(plan, _state(plan, 4, quantity=2), {"add_signal_price": "102", "last_entry_price": "100"}))
    losing = profile.manage(ManagementContext(plan, _state(plan, 4, quantity=2), {"add_signal_price": "99", "last_entry_price": "100"}))
    exhausted = profile.manage(ManagementContext(plan, _state(plan, 4, quantity=2, adds=2), {"add_signal_price": "102", "last_entry_price": "100"}))

    assert allowed.actions == (AddToTrade("trade-1:atr-add:4", "trade-1", 4, "atr-profitable-advance", 1),)
    assert losing.actions == ()
    assert exhausted.actions == ()
