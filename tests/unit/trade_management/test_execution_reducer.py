from decimal import Decimal

import pytest

from src.trade_journal.reducer import AccountState, PositionState, apply_fill


def test_increasing_fill_uses_exact_decimal_weighted_average_and_fee():
    position, account = apply_fill(
        PositionState(2, Decimal("100.1"), Decimal("0"), Decimal("1")),
        AccountState(Decimal("999"), Decimal("999"), Decimal("0"), Decimal("1")),
        side="BUY", action_type="ADD", quantity=1, price=Decimal("100.4"), fee=Decimal("0.2"),
    )

    assert position == PositionState(3, Decimal("100.2"), Decimal("0"), Decimal("1.2"))
    assert account.balance == Decimal("998.8")
    assert position.net_realized_pnl == Decimal("-1.2")
    assert account.net_realized_pnl == Decimal("-1.2")


def test_reducing_short_fill_realizes_profit_and_clears_average_at_zero():
    position, account = apply_fill(
        PositionState(2, Decimal("100"), Decimal("0"), Decimal("0")),
        AccountState(Decimal("1000"), Decimal("1000"), Decimal("0"), Decimal("0")),
        side="SELL", action_type="TARGET:tp-1", quantity=2, price=Decimal("96"), fee=Decimal("1"),
    )

    assert position == PositionState(0, None, Decimal("8"), Decimal("1"))
    assert account == AccountState(Decimal("1007"), Decimal("1007"), Decimal("8"), Decimal("1"))
    assert position.net_realized_pnl == Decimal("7")
    assert account.net_realized_pnl == Decimal("7")


def test_reducing_more_than_open_position_is_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        apply_fill(
            PositionState(1, Decimal("100"), Decimal("0"), Decimal("0")),
            AccountState(Decimal("1000"), Decimal("1000"), Decimal("0"), Decimal("0")),
            side="BUY", action_type="CLOSE", quantity=2, price=Decimal("99"), fee=Decimal("0"),
        )
