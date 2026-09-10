"""Вспомогательные билдеры серий свечей для unit-тестов рыночной структуры.

Строят детерминированные зигзаг-серии, в которых свинги X,A,B,C
располагаются ровно на ожидаемых индексах (X на 3, A на 6, B на 9, C на 12).
"""

import numpy as np
import pandas as pd


def xabcd_series(length, pivots):
    """Построить серию свечей с заданными разворотами.

    pivots: список (index, kind, price), kind — 'high' или 'low'.
    Цены между разворотами интерполируются монотонно, поэтому каждая
    точка — строгий локальный экстремум в окне свинг-детектора.
    """
    closes = np.full(length, float(pivots[0][2]))
    for (i0, _, p0), (i1, _, p1) in zip(pivots, pivots[1:]):
        closes[i0 : i1 + 1] = np.linspace(p0, p1, i1 - i0 + 1)
    if pivots:
        closes[pivots[-1][0] :] = pivots[-1][2]

    high = closes + 0.6
    low = closes - 0.6
    for idx, kind, price in pivots:
        if kind == "high":
            high[idx] = price
        else:
            low[idx] = price

    return pd.DataFrame(
        {
            "open": closes,
            "high": high,
            "low": low,
            "close": closes,
            "volume": [1000] * length,
        }
    )


def bullish_series(x=103.0, a=80.0, b=91.5, c=84.0, tail=88.0):
    """Бычья формация: X-вершина, A-впадина, B-вершина, C-впадина.

    X подтверждается в окне left/right=2 (индекс 3), серия из 19 баров
    с подводом к X и отходом от C.
    """
    return xabcd_series(
        19,
        [
            (0, "low", x - 3.0),
            (3, "high", x),
            (6, "low", a),
            (9, "high", b),
            (12, "low", c),
            (15, "high", tail),
        ],
    )


def bearish_series(x=80.0, a=103.0, b=91.5, c=98.4, tail=95.0):
    """Медвежья формация: X-впадина, A-вершина, B-впадина, C-вершина."""
    return xabcd_series(
        19,
        [
            (0, "high", x + 3.0),
            (3, "low", x),
            (6, "high", a),
            (9, "low", b),
            (12, "high", c),
            (15, "low", tail),
        ],
    )