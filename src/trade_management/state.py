from dataclasses import replace

from src.trade_management.models import TradePhase, TradeState


_ALLOWED_TRANSITIONS = {
    TradePhase.PLANNED: {TradePhase.ENTRY_PENDING, TradePhase.CANCELLED},
    TradePhase.ENTRY_PENDING: {TradePhase.OPEN, TradePhase.BUILDING, TradePhase.CANCELLED},
    TradePhase.OPEN: {TradePhase.BUILDING, TradePhase.REDUCING, TradePhase.PARTIALLY_CLOSED, TradePhase.CLOSED,
                      TradePhase.ERROR},
    TradePhase.BUILDING: {TradePhase.REDUCING, TradePhase.CLOSED},
    TradePhase.REDUCING: {TradePhase.PARTIALLY_CLOSED, TradePhase.CLOSED, TradePhase.ERROR},
    TradePhase.CLOSED: set(),
    TradePhase.CANCELLED: set(),
    TradePhase.PARTIALLY_CLOSED: {TradePhase.PARTIALLY_CLOSED, TradePhase.CLOSED, TradePhase.ERROR},
    TradePhase.REJECTED: set(),
    TradePhase.ERROR: set(),
}


def transition(state: TradeState, phase: TradePhase) -> TradeState:
    """Move a trade through its irreversible lifecycle with optimistic revisioning."""
    if phase not in _ALLOWED_TRANSITIONS[state.phase]:
        raise ValueError(f"invalid trade transition: {state.phase} -> {phase}")
    return replace(state, phase=phase, state_revision=state.state_revision + 1)
