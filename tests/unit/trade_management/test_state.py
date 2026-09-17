import pytest

from src.trade_management import TradePhase, TradeState
from src.trade_management.state import transition


def test_valid_transition_advances_optimistic_revision():
    state = transition(TradeState("trade-1"), TradePhase.ENTRY_PENDING)

    assert state.phase is TradePhase.ENTRY_PENDING
    assert state.state_revision == 1


@pytest.mark.parametrize("phase", [TradePhase.OPEN, TradePhase.CLOSED])
def test_impossible_transition_is_rejected(phase):
    with pytest.raises(ValueError, match="invalid trade transition"):
        transition(TradeState("trade-1"), phase)


def test_terminal_phase_cannot_be_reopened():
    cancelled = transition(TradeState("trade-1"), TradePhase.CANCELLED)

    with pytest.raises(ValueError, match="invalid trade transition"):
        transition(cancelled, TradePhase.ENTRY_PENDING)
