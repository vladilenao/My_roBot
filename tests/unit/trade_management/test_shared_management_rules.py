from decimal import Decimal

import pytest

from src.trade_management.actions import CancelEntry, CloseTrade, ReduceTrade
from src.trade_management.models import TradePhase, TradeState
from src.trade_management.profiles.rules import (
    add_quantity,
    prioritize_management_actions,
    reduce_late_increase_fill,
)


def _state(*, phase=TradePhase.OPEN, quantity=4, adds=0, targets=frozenset()):
    return TradeState(
        "trade-1",
        phase,
        7,
        quantity,
        Decimal("100"),
        targets,
        adds,
    )


def test_add_rules_enforce_maximum_fraction_and_profitable_only():
    parameters = {"max_adds": 2, "add_fraction": "0.5"}

    assert add_quantity(_state(), parameters, "101", "BUY") == 2
    assert add_quantity(_state(adds=2), parameters, "101", "BUY") is None
    assert add_quantity(_state(), parameters, "100", "BUY") is None
    assert add_quantity(_state(quantity=1), parameters, "101", "BUY") is None


@pytest.mark.parametrize("state", [
    _state(phase=TradePhase.REDUCING),
    _state(targets=frozenset({"tp-1"})),
])
def test_add_rules_forbid_averaging_after_the_first_reduction(state):
    assert add_quantity(state, {"max_adds": 2, "add_fraction": "0.5"}, "101", "BUY") is None


def test_first_reduction_cancels_pending_increase_intention():
    actions = prioritize_management_actions(
        _state(phase=TradePhase.REDUCING), (), pending_increase=True
    )

    assert actions == (
        CancelEntry("trade-1:cancel-pending-increase:7", "trade-1", 7, "first-reduction-filled"),
    )


def test_full_exit_wins_over_simultaneous_partial_reduce():
    state = _state()
    actions = prioritize_management_actions(
        state,
        (
            ReduceTrade("reduce", "trade-1", 7, "partial", 1),
            CloseTrade("close", "trade-1", 7, "full"),
        ),
    )

    assert actions == (CloseTrade("close", "trade-1", 7, "full"),)


def test_late_increase_fill_after_cancel_is_reduced_back():
    state = _state(phase=TradePhase.REDUCING, quantity=5)

    action = reduce_late_increase_fill(state, 1)

    assert action == ReduceTrade(
        "trade-1:late-increase-reduce:7",
        "trade-1",
        7,
        "late-increase-fill-after-cancel",
        1,
    )


def test_late_increase_reduction_requires_accepted_fill_in_reducing_phase():
    with pytest.raises(ValueError, match="first reduction"):
        reduce_late_increase_fill(_state(), 1)
