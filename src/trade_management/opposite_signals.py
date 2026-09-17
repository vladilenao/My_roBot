"""Pure handling of raw signals opposite to an owning trade."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import CloseTrade
from src.trade_management.models import TradePhase, TradePlan, TradeState


class OppositeSignalPolicy(StrEnum):
    IGNORE = "ignore"
    CLOSE = "close"
    REVERSE = "reverse"


@dataclass(frozen=True)
class OppositeSignalResult:
    """The owning-trade action selected from a raw strategy event."""

    actions: tuple[CloseTrade, ...] = ()
    reverse_requested: bool = False


@dataclass(frozen=True)
class ReverseAdmission:
    """Explicit result of the fresh signal, filter, and risk admission."""

    candidate: object | None = None
    reason: str | None = None


def handle_raw_opposite_signal(
    plan: TradePlan,
    state: TradeState,
    raw_signal: Decision,
    policy: OppositeSignalPolicy | str = OppositeSignalPolicy.CLOSE,
    *,
    reverse_enabled: bool = False,
) -> OppositeSignalResult:
    """Return an owner-addressed close for an opposite raw signal.

    The input is intentionally the unfiltered strategy event.  A reverse only
    records its request here; reopening must wait for confirmed closure and use
    :func:`readmit_reverse`.
    """
    selected_policy = OppositeSignalPolicy(policy)
    if not _is_opposite(plan, raw_signal) or state.phase in {
        TradePhase.CLOSED,
        TradePhase.CANCELLED,
    }:
        return OppositeSignalResult()
    if selected_policy is OppositeSignalPolicy.IGNORE:
        return OppositeSignalResult()

    signal_id = raw_signal.event_id
    if not signal_id:
        raise ValueError("raw opposite signal requires event_id")
    close = CloseTrade(
        f"{plan.trade_id}:raw-opposite-close:{signal_id}:{state.state_revision}",
        plan.trade_id,
        state.state_revision,
        "raw-opposite-signal",
    )
    return OppositeSignalResult(
        actions=(close,),
        reverse_requested=(
            selected_policy is OppositeSignalPolicy.REVERSE and reverse_enabled
        ),
    )


def readmit_reverse(
    state: TradeState,
    raw_signal: Decision,
    blocking_instrument_exposure_ids: frozenset[str] | set[str] | tuple[str, ...],
    readmit: Callable[[Decision], ReverseAdmission],
) -> ReverseAdmission:
    """Request a new candidate only after confirmed closure and clear exposure.

    ``readmit`` is deliberately outside this policy so a fresh signal check,
    entry filter, and portfolio-risk check cannot be bypassed by reversal.
    """
    if state.phase is not TradePhase.CLOSED or state.quantity != 0:
        return ReverseAdmission(reason="close-not-confirmed")
    if blocking_instrument_exposure_ids:
        return ReverseAdmission(reason="opposite-exposure")
    return readmit(raw_signal)


def _is_opposite(plan: TradePlan, signal: Decision) -> bool:
    return (
        plan.side == SignalType.BUY.value and signal.signal_type is SignalType.SELL
    ) or (
        plan.side == SignalType.SELL.value and signal.signal_type is SignalType.BUY
    )
