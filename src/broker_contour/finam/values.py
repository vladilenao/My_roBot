"""Числа Finam Trade API: ``google.type.Decimal`` и ``google.type.Money`` → ``Decimal``."""

from __future__ import annotations

from decimal import Decimal


def decimal_of(value) -> Decimal | None:
    """``google.type.Decimal``; пустое значение — ``None``."""
    text = getattr(value, "value", "") if value is not None else ""
    return Decimal(text) if text else None


def money_of(value) -> Decimal:
    """``google.type.Money``: ``units`` + ``nanos``."""
    return Decimal(value.units) + Decimal(value.nanos) / Decimal(10**9)
