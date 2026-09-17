from decimal import Decimal

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import AddToTrade, CloseTrade, MoveStop, ReduceTrade
from src.trade_management.models import ProfileSnapshot, TradePhase, TradeState
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult
from src.trade_management.profiles.ma_cloud import MaCloudProfile


def _planning(side=SignalType.BUY, *, ma10="103", ma40="100"):
    return PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NG-10.26",
        signal=Decision(side, 104.0, event_id="signal-1"),
        profile=ProfileSnapshot("ma_cloud", "1", {"buffer_ticks": 1, "max_adds": 2}),
        market={"price_step": "1", "ma10": ma10, "ma40": ma40},
    )


def _state(plan, revision, *, quantity=2, stop=None, phase=TradePhase.OPEN, adds=0):
    return TradeState(
        trade_id=plan.trade_id,
        phase=phase,
        state_revision=revision,
        quantity=quantity,
        average_price=Decimal("104"),
        confirmed_stop=Decimal(stop) if stop else plan.stop_price,
        add_count=adds,
    )


def test_ma_cloud_plans_targetless_stop_behind_the_correct_cloud_boundary():
    long = MaCloudProfile().plan(_planning())
    short = MaCloudProfile().plan(_planning(SignalType.SELL, ma10="105", ma40="108"))

    assert long.stop_price == Decimal("99")
    assert long.targets == ()
    assert short.stop_price == Decimal("109")


def test_ma_cloud_rejects_plan_without_warmed_moving_averages():
    result = MaCloudProfile().plan(_planning(ma10=None))

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "insufficient-history"}


def test_ma_cloud_moves_documented_long_stop_monotonically():
    profile = MaCloudProfile()
    plan = profile.plan(_planning())
    cases = ((1, "104", "101", "99", "100"), (2, "106", "102", "100", "101"), (3, "107", "103", "101", "102"), (4, "106", "104", "102", "103"))

    for revision, ma10, ma40, current, expected in cases:
        result = profile.manage(ManagementContext(
            plan, _state(plan, revision, stop=current),
            {"price_step": "1", "ma10": ma10, "ma40": ma40, "close": "108", "bar_id": f"t{revision}"},
        ))
        assert result.actions == (
            MoveStop(f"trade-1:ma-cloud-stop:t{revision}", "trade-1", revision, "ma-cloud-protective-stop", Decimal(expected)),
        )

    lower = profile.manage(ManagementContext(
        plan, _state(plan, 5, stop="103"),
        {"price_step": "1", "ma10": "104", "ma40": "102", "close": "108", "bar_id": "t5"},
    ))
    assert lower.actions == ()


def test_ma40_full_exit_has_priority_over_ma10_partial_exit():
    profile = MaCloudProfile()
    plan = profile.plan(_planning())

    result = profile.manage(ManagementContext(
        plan, _state(plan, 5, quantity=3, stop="103"),
        {"price_step": "1", "ma10": "105", "ma40": "104", "close": "103.5", "bar_id": "t5"},
    ))

    assert result.actions == (
        CloseTrade("trade-1:ma40-exit:t5", "trade-1", 5, "ma40-opposite-close"),
    )


def test_ma10_or_cloud_reduces_one_actual_contract_once_per_bar_but_never_q1():
    profile = MaCloudProfile()
    plan = profile.plan(_planning())
    market = {"price_step": "1", "ma10": "107", "ma40": "103", "close": "105", "bar_id": "t3"}

    result = profile.manage(ManagementContext(plan, _state(plan, 3, quantity=2, stop="101"), market))
    repeated = profile.manage(ManagementContext(plan, _state(plan, 4, quantity=2, stop="101"), market))
    q1 = profile.manage(ManagementContext(plan, _state(plan, 3, quantity=1, stop="101"), market))

    expected_reduce = ReduceTrade("trade-1:ma-partial-exit:t3", "trade-1", 3, "ma10-or-cloud-partial-exit", 1)
    assert result.actions[-1] == expected_reduce
    assert repeated.actions[-1].command_id == expected_reduce.command_id
    assert q1.actions == (MoveStop("trade-1:ma-cloud-stop:t3", "trade-1", 3, "ma-cloud-protective-stop", Decimal("102")),)


def test_ma_cloud_add_requires_profitable_retest_and_is_limited_to_half_actual_quantity():
    profile = MaCloudProfile()
    plan = profile.plan(_planning())
    market = {"price_step": "1", "ma10": "106", "ma40": "104", "close": "107", "cloud_retest": True, "add_signal_price": "106", "bar_id": "add"}

    allowed = profile.manage(ManagementContext(plan, _state(plan, 6, quantity=3, stop="102"), market))
    q1 = profile.manage(ManagementContext(plan, _state(plan, 6, quantity=1, stop="103"), market))
    reduced = profile.manage(ManagementContext(plan, _state(plan, 6, quantity=3, stop="103", phase=TradePhase.REDUCING), market))
    losing = profile.manage(ManagementContext(plan, _state(plan, 6, quantity=3, stop="103"), {**market, "add_signal_price": "103"}))

    assert allowed.actions == (
        MoveStop("trade-1:ma-cloud-stop:add", "trade-1", 6, "ma-cloud-protective-stop", Decimal("103")),
        AddToTrade("trade-1:ma-cloud-add:add", "trade-1", 6, "ma-cloud-profitable-retest", 1),
    )
    assert q1.actions == ()
    assert reduced.actions == ()
    assert losing.actions == ()
