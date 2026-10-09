"""Рыночный вид, из которого профили читают волатильность и расходы.

Проверяется форма view: профиль строит стоп по тому, что здесь есть, поэтому
отсутствующее значение должно отсутствовать, а не превращаться в ноль.
"""

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from src.trade_management.pipeline import build_management_market

CONTRACT = SimpleNamespace(price_step=Decimal("0.001"), step_cost=Decimal("8.4"))


def _frame(closes, *, highs=None, lows=None) -> pd.DataFrame:
    index = pd.date_range("2026-09-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "datetime": index,
            "open": closes,
            "high": highs if highs is not None else [c + 0.002 for c in closes],
            "low": lows if lows is not None else [c - 0.002 for c in closes],
            "close": closes,
        }
    )


def test_bar_range_is_the_width_of_the_last_closed_bar():
    frame = _frame([3.020 + index * 0.001 for index in range(20)])

    market = build_management_market(contract=CONTRACT, frame=frame)

    assert float(market["bar_range"]) == pytest.approx(0.004)
    assert market["high"] - market["low"] == market["bar_range"]


def test_bar_range_is_absent_when_the_bar_has_no_high_or_low():
    frame = _frame([3.02] * 20, highs=[None] * 20, lows=[3.018] * 20)

    market = build_management_market(contract=CONTRACT, frame=frame)

    assert "bar_range" not in market


def test_zero_width_bar_does_not_invent_a_range():
    """Плоский бар — это нулевой диапазон, а не неизвестный."""
    frame = _frame([3.02] * 20, highs=[3.02] * 20, lows=[3.02] * 20)

    market = build_management_market(contract=CONTRACT, frame=frame)

    assert market["bar_range"] == Decimal("0")


def test_costs_default_to_zero_and_are_reported_per_side():
    market = build_management_market(contract=CONTRACT, frame=_frame([3.02] * 20))

    assert market["entry_cost"] == Decimal("0")
    assert market["exit_cost"] == Decimal("0")
    assert market["slippage_cost"] == Decimal("0")

    market = build_management_market(
        contract=CONTRACT, frame=_frame([3.02] * 20), commission=Decimal("1.5"), slippage=Decimal("1"),
    )

    assert market["entry_cost"] == Decimal("1.5")
    assert market["exit_cost"] == Decimal("1.5")
    assert market["slippage_cost"] == Decimal("1")


def test_atr_needs_a_warmed_up_history():
    """Недостаточная история — это отсутствие ATR, а не ноль: ноль снял бы границу стоп��."""
    market = build_management_market(
        contract=CONTRACT, frame=_frame([3.02] * 5), profile_parameters={"atr_period": 14},
    )

    assert market["atr"] is None
