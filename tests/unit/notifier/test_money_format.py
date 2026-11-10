"""Деньги в карточках сделки Telegram до копеек (fix-trade-close-accuracy)."""

from decimal import Decimal

from src.notifier.telegram_templates import money


def test_close_card_amounts_are_rounded_with_groups():
    assert money(Decimal("15349.174572"), signed=True) == "+15 349,17 ₽"
    assert money(Decimal("17084.8288"), signed=True) == "+17 084,83 ₽"
    assert money(Decimal("-1735.654228"), signed=True) == "−1 735,65 ₽"


def test_whole_rubles_zero_and_other_units():
    assert money(Decimal("92"), signed=True) == "+92 ₽"
    assert money(Decimal("0.001"), signed=True) == "0 ₽"
    assert money(Decimal("-0.001"), signed=True) == "0 ₽"
    assert money(Decimal("12.5"), units="RAW") == "12,50 в единицах цены"
    assert money(None) == "неизвестно"
