from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

from src.strategies.contracts import Decision, SignalType
from src.trade_management.actions import CloseTrade
from src.trade_management.models import ProfileSnapshot, TradePhase, TradePlan, TradeState
from src.trade_management.opposite_signals import (
    OppositeSignalPolicy,
    ReverseAdmission,
    handle_raw_opposite_signal,
    readmit_reverse,
)


def _plan(trade_id="trade-1"):
    return TradePlan(
        trade_id=trade_id,
        assignment_id="assignment-1",
        instrument_id="instrument-1",
        side="BUY",
        signal_id="entry-1",
        reference_entry=Decimal("100"),
        stop_price=Decimal("99"),
        targets=(),
        profile=ProfileSnapshot("levels_rr", "1", {}),
        created_at=datetime(2026, 1, 1),
    )


def _open_state(trade_id="trade-1"):
    return TradeState(trade_id, TradePhase.OPEN, 4, 1, Decimal("100"))


def _opposite_signal():
    return Decision(SignalType.SELL, 100.0, event_id="raw-sell-1")


def test_raw_opposite_signal_closes_own_trade_by_default_without_filtering():
    result = handle_raw_opposite_signal(_plan(), _open_state(), _opposite_signal())

    assert result.actions == (
        CloseTrade(
            "trade-1:raw-opposite-close:raw-sell-1:4",
            "trade-1",
            4,
            "raw-opposite-signal",
        ),
    )
    assert not result.reverse_requested


def test_ignore_policy_keeps_own_trade_open():
    result = handle_raw_opposite_signal(
        _plan(), _open_state(), _opposite_signal(), OppositeSignalPolicy.IGNORE
    )

    assert result.actions == ()


def test_reverse_is_disabled_by_default_but_can_request_post_close_readmission():
    disabled = handle_raw_opposite_signal(
        _plan(), _open_state(), _opposite_signal(), OppositeSignalPolicy.REVERSE
    )
    enabled = handle_raw_opposite_signal(
        _plan(),
        _open_state(),
        _opposite_signal(),
        OppositeSignalPolicy.REVERSE,
        reverse_enabled=True,
    )

    assert disabled.actions == enabled.actions
    assert not disabled.reverse_requested
    assert enabled.reverse_requested


def test_reverse_reopens_only_after_confirmed_close_without_blocking_exposure():
    readmit = Mock(return_value=ReverseAdmission(candidate="new-short"))
    closed = TradeState("trade-1", TradePhase.CLOSED, 5)

    result = readmit_reverse(closed, _opposite_signal(), (), readmit)

    assert result == ReverseAdmission(candidate="new-short")
    readmit.assert_called_once_with(_opposite_signal())


def test_reverse_does_not_readmit_before_close_or_while_instrument_is_blocked():
    readmit = Mock(return_value=ReverseAdmission(candidate="new-short"))

    pending = readmit_reverse(_open_state(), _opposite_signal(), (), readmit)
    blocked = readmit_reverse(
        TradeState("trade-1", TradePhase.CLOSED, 5),
        _opposite_signal(),
        ("neighbor-trade",),
        readmit,
    )

    assert pending == ReverseAdmission(reason="close-not-confirmed")
    assert blocked == ReverseAdmission(reason="opposite-exposure")
    readmit.assert_not_called()


def test_opposite_close_preserves_neighboring_trade():
    result = handle_raw_opposite_signal(_plan("trade-1"), _open_state("trade-1"), _opposite_signal())

    assert result.actions[0].trade_id == "trade-1"
    assert result.actions[0].trade_id != "neighbor-trade"
