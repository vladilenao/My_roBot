"""Расчёт фибо-уровней ретрейсмента и расширения, проверка попадания в зону.

Уровни считаются для волны между двумя точками. Для нисходящей волны
X(высокий)→A(низкий) ретрейсмент r% от точки A равен A + r*(X−A)
(значение выше A, обратно к X), расширение 161.8% равно A + 1.618*(X−A).
Для восходящей волны формулы зеркальны (от точки A вниз).
"""

from __future__ import annotations


def retracement_level(a: float, x: float, ratio: float) -> float:
    """Уровень ретрейсмента `ratio` волны X→A.

    Нисходящая волна (x >= a): a + ratio*(x - a).
    Восходящая волна (a >= x): a - ratio*(a - x).
    """
    return a + ratio * (x - a)


def extension_level(a: float, x: float, ratio: float = 1.618) -> float:
    """Уровень расширения `ratio` волны X→A (цель за точкой A).

    Нисходящая волна (x >= a): выше A → a + ratio*(x - a).
    Восходящая волна (a >= x): ниже A → a - ratio*(a - x).
    """
    return a - ratio * (a - x)


def in_zone(price: float, level: float, amplitude: float, tolerance: float) -> bool:
    """Проверяет попадание `price` в фибо-зону уровня с допуском.

    Допустимый интервал: [level*(1 - tolerance), level*(1 + tolerance)], где
    `amplitude` — амплитуда контрастного конца волны (для относительности).
    """
    if amplitude == 0:
        return price == level
    margin = level * tolerance
    return level - margin <= price <= level + margin
