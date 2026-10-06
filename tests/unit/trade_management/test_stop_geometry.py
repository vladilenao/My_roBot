"""Границы защитного стопа: пол, потолок, округление, отсутствующий ATR.

Каждый тест отвечает на один вопрос трейдера — почему стоп оказался там, где
он оказался, — поэтому проверяется и расстояние, и основание выбора.
"""

from decimal import Decimal

import pytest

from src.trade_management.models import StopBasis
from src.trade_management.profiles.geometry import (
    StopPolicy,
    resolve_stop,
)


def _policy(**overrides) -> StopPolicy:
    """Политика по умолчанию для теста: пол из ATR, потолок отключён."""
    values = {"min_stop_atr": Decimal("1.5"), "stop_beyond_bar": 0, "max_stop_atr": None}
    values.update(overrides)
    return StopPolicy(**values)


def test_stop_inside_the_noise_is_widened_to_the_atr_floor():
    """Уровень в трёх тиках от входа не выживает на инструменте с ATR 12 тиков."""
    geometry = resolve_stop(
        Decimal("3.030"), "BUY", Decimal("0.003"), _policy(),
        atr=Decimal("0.012"), price_step=Decimal("0.001"),
    )

    assert geometry.price == Decimal("3.012")
    assert geometry.distance == Decimal("0.018")
    assert geometry.basis is StopBasis.ATR_FLOOR


def test_ticks_floor_holds_when_atr_is_unavailable():
    """Тик — единственная единица, которой инструмент умеет измерять защиту."""
    geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("2"), _policy(min_stop_atr=0, stop_beyond_bar=0),
        price_step=Decimal("0.5"),
    )

    assert geometry.price == Decimal("95")
    assert geometry.basis is StopBasis.ATR_FLOOR


def test_bar_range_floor_puts_the_stop_outside_the_bar_that_made_it():
    geometry = resolve_stop(
        Decimal("3.030"), "BUY", Decimal("0.005"), _policy(stop_beyond_bar=1),
        atr=Decimal("0.012"), bar_range=Decimal("0.050"), price_step=Decimal("0.001"),
    )

    assert geometry.price == Decimal("2.980")
    assert geometry.basis is StopBasis.ATR_FLOOR


def test_stop_beyond_the_cap_is_shortened_to_it():
    geometry = resolve_stop(
        Decimal("2.79973"), "SELL", Decimal("0.076"),
        _policy(max_stop_atr=Decimal("3")),
        atr=Decimal("0.010260"), price_step=Decimal("0.001"),
    )

    assert geometry.price == Decimal("2.831")
    assert geometry.basis is StopBasis.ATR_CAP


def test_cap_does_not_bind_without_atr():
    """Потолок выражен в ATR: без ATR его не из чего посчитать, и он снимается."""
    geometry = resolve_stop(
        Decimal("100"), "SELL", Decimal("50"),
        _policy(min_stop_atr=0, stop_beyond_bar=0, max_stop_atr=Decimal("3")),
        price_step=Decimal("1"),
    )

    assert geometry.price == Decimal("150")
    assert geometry.basis is StopBasis.STRUCTURAL


def test_missing_atr_keeps_the_structural_stop():
    geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("4"),
        _policy(min_stop_ticks=0, stop_beyond_bar=0),
        price_step=Decimal("1"),
    )

    assert geometry.price == Decimal("96")
    assert geometry.basis is StopBasis.STRUCTURAL


def test_missing_atr_keeps_the_ticks_floor():
    """Недоступный ATR снимает свою границу, а не границу шага цены."""
    geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("1"),
        _policy(min_stop_ticks=Decimal("10"), stop_beyond_bar=0),
        price_step=Decimal("1"),
    )

    assert geometry.price == Decimal("90")
    assert geometry.basis is StopBasis.ATR_FLOOR


def test_missing_atr_leaves_the_independent_floors_intact():
    """Независимые полы считаются и без ATR: 500 − 10 = 490 по десяти тикам."""
    policy = _policy(min_stop_ticks=Decimal("10"), stop_beyond_bar=0)

    raised = resolve_stop(
        Decimal("500"), "BUY", Decimal("3"), policy, price_step=Decimal("1")
    )
    structural = resolve_stop(
        Decimal("500"), "BUY", Decimal("25"), policy, price_step=Decimal("1")
    )

    assert raised.distance == Decimal("10")
    assert raised.price == Decimal("490")
    assert raised.basis is StopBasis.ATR_FLOOR
    assert structural.distance == Decimal("25")
    assert structural.price == Decimal("475")
    assert structural.basis is StopBasis.STRUCTURAL


def test_conflicting_floor_and_cap_gives_the_cap_the_priority():
    """Пол 5×ATR = 10 при потолке 3×ATR = 6: выигрывает потолок.

    Несовместимые границы — текущий алгоритм ограничивает сверху; отказ
    вместо молчливого приоритета потолка вводит следующий change.
    """
    geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("1"),
        _policy(min_stop_atr=Decimal("5"), max_stop_atr=Decimal("3")),
        atr=Decimal("2"), price_step=Decimal("1"),
    )

    assert geometry.distance == Decimal("6")
    assert geometry.price == Decimal("94")
    assert geometry.basis is StopBasis.ATR_CAP


def test_price_is_rounded_away_from_the_entry():
    geometry = resolve_stop(
        Decimal("3.030"), "BUY", Decimal("0.0175"),
        _policy(min_stop_atr=0, min_stop_ticks=0, stop_beyond_bar=0),
        price_step=Decimal("0.001"),
    )

    assert geometry.price == Decimal("3.012")
    assert geometry.distance == Decimal("0.018")


def test_short_is_the_mirror_of_long():
    long_geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("3"), _policy(min_stop_ticks=0),
        atr=Decimal("4"), price_step=Decimal("1"),
    )
    short_geometry = resolve_stop(
        Decimal("100"), "SELL", Decimal("3"), _policy(min_stop_ticks=0),
        atr=Decimal("4"), price_step=Decimal("1"),
    )

    assert long_geometry.price == Decimal("94")
    assert short_geometry.price == Decimal("106")
    assert long_geometry.distance == short_geometry.distance


def test_distance_is_never_narrower_than_one_tick():
    """Потолок уже одного тика не должен прижать стоп на сам вход."""
    geometry = resolve_stop(
        Decimal("100"), "BUY", Decimal("1"),
        _policy(min_stop_atr=0, min_stop_ticks=0, stop_beyond_bar=0, max_stop_atr=Decimal("0.1")),
        atr=Decimal("1"), price_step=Decimal("1"),
    )

    assert geometry.price == Decimal("99")
    assert geometry.distance == Decimal("1")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_unknown_side_is_refused(side):
    with pytest.raises(ValueError, match="side"):
        resolve_stop(Decimal("100"), "HOLD", Decimal("1"), _policy(), price_step=Decimal("1"))


def test_policy_falls_back_to_shipped_defaults():
    policy = StopPolicy.from_parameters({})

    assert policy.min_stop_atr == Decimal("1.5")
    assert policy.min_stop_ticks == Decimal("10")
    assert policy.stop_beyond_bar == Decimal("1")
    assert policy.max_stop_atr == Decimal("6")


def test_policy_reads_explicit_values_and_keeps_no_cap():
    policy = StopPolicy.from_parameters(
        {"min_stop_atr": "0", "min_stop_ticks": 0, "stop_beyond_bar": "2.5", "max_stop_atr": None}
    )

    assert policy.min_stop_atr == Decimal("0")
    assert policy.min_stop_ticks == Decimal("0")
    assert policy.stop_beyond_bar == Decimal("2.5")
    assert policy.cap(Decimal("1")) is None
