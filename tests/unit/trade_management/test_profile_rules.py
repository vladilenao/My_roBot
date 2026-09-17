from decimal import Decimal

import pytest

from src.trade_management.profiles.rules import (
    TargetAllocation,
    allocate_target_quantities,
    cost_aware_break_even,
    initial_stop,
    initial_target,
    round_price,
    validate_stop,
)
from src.trade_management.models import TargetPlan


@pytest.mark.parametrize(("step", "price", "expected_floor", "expected_ceiling"), [
    ("1", "100.5", "100", "101"),
    ("0.1", "100.01", "100.0", "100.1"),
    ("0.125", "100.1876", "100.125", "100.250"),
])
def test_round_price_supports_integer_decimal_and_fractional_steps(
    step, price, expected_floor, expected_ceiling
):
    assert round_price(Decimal(price), Decimal(step), "floor") == Decimal(expected_floor)
    assert round_price(Decimal(price), Decimal(step), "ceiling") == Decimal(expected_ceiling)


def test_round_price_keeps_exact_tick_at_rounding_boundary():
    assert round_price("100.125", "0.125", "floor") == Decimal("100.125")
    assert round_price("100.125", "0.125", "ceiling") == Decimal("100.125")


@pytest.mark.parametrize(("side", "stop", "target", "expected_stop", "expected_target"), [
    ("LONG", "99.94", "104.06", "99.9", "104.0"),
    ("SHORT", "100.06", "95.94", "100.1", "96.0"),
    ("BUY", "99.94", "104.06", "99.9", "104.0"),
    ("SELL", "100.06", "95.94", "100.1", "96.0"),
])
def test_initial_stop_and_target_are_mirrored_by_direction(
    side, stop, target, expected_stop, expected_target
):
    assert initial_stop(stop, "0.1", side) == Decimal(expected_stop)
    assert initial_target(target, "0.1", side) == Decimal(expected_target)


@pytest.mark.parametrize(("side", "stop"), [
    ("LONG", "100"), ("LONG", "100.1"), ("SHORT", "100"), ("SHORT", "99.9"),
])
def test_validate_stop_rejects_non_protective_levels(side, stop):
    with pytest.raises(ValueError, match="loss side"):
        validate_stop("100", stop, side)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_initial_rules_reject_invalid_prices(value):
    with pytest.raises(ValueError):
        initial_stop(value, "0.1", "LONG")


def test_cost_aware_break_even_rounds_toward_covering_long_costs():
    assert cost_aware_break_even(
        "100", "LONG", "0.125", "12.5", 2, entry_cost="10", exit_cost="10", slippage_cost="1"
    ) == Decimal("100.125")


def test_cost_aware_break_even_rounds_toward_covering_short_costs():
    assert cost_aware_break_even(
        "100", "SHORT", "0.125", "12.5", 2, entry_cost="10", exit_cost="10", slippage_cost="1"
    ) == Decimal("99.875")


def test_cost_aware_break_even_rejects_invalid_quantity_and_costs():
    with pytest.raises(ValueError, match="quantity"):
        cost_aware_break_even("100", "LONG", "1", "100", 0)
    with pytest.raises(ValueError, match="costs"):
        cost_aware_break_even("100", "LONG", "1", "100", 1, exit_cost="-1")


TARGETS = (
    TargetPlan("tp-1", Decimal("104"), Decimal("0.5")),
    TargetPlan("tp-2", Decimal("108"), Decimal("0.5")),
)


@pytest.mark.parametrize(("filled_quantity", "expected"), [
    (1, (TargetAllocation("tp-2", 1),)),
    (2, (TargetAllocation("tp-1", 1), TargetAllocation("tp-2", 1))),
    (3, (TargetAllocation("tp-1", 1), TargetAllocation("tp-2", 2))),
])
def test_target_quantities_floor_intermediate_shares_and_give_last_target_remainder(
    filled_quantity, expected
):
    allocation = allocate_target_quantities(filled_quantity, TARGETS)

    assert allocation.targets == expected
    assert allocation.trailing_quantity == 0


def test_target_quantities_use_partial_entry_fill_not_planned_quantity():
    allocation = allocate_target_quantities(2, TARGETS)

    assert allocation.targets == (
        TargetAllocation("tp-1", 1),
        TargetAllocation("tp-2", 1),
    )


@pytest.mark.parametrize(("filled_quantity", "expected_targets", "trailing_quantity"), [
    (1, (), 1),
    (2, (TargetAllocation("tp-1", 1),), 1),
    (3, (TargetAllocation("tp-1", 1),), 2),
])
def test_atr_trend_retains_remainder_for_trailing_without_zero_tp_orders(
    filled_quantity, expected_targets, trailing_quantity
):
    allocation = allocate_target_quantities(
        filled_quantity,
        TARGETS[:1],
        retain_remainder_for_trailing=True,
    )

    assert allocation.targets == expected_targets
    assert allocation.trailing_quantity == trailing_quantity
    assert allocation.trailing_activated is (filled_quantity > 0)
    assert all(target.quantity > 0 for target in allocation.targets)
