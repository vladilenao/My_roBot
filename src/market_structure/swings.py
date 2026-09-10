"""Детекторы свинговых вершин и впадин (фракталы по соседним барам).

Свинговая вершина — бар, максимум которого выше максимумов `left` соседей
слева и `right` соседей справа. Свинговая впадина — бар, минимум которого
ниже минимумов соседей с обеих сторон.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SwingKind(Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class SwingPoint:
    """Точка разворота: индекс бара, цена и тип (вершина/впадина)."""

    index: int
    price: float
    kind: SwingKind


@dataclass(frozen=True)
class SwingDetector:
    """Статистически чистый детектор свинговых вершин и впадин."""

    left: int = 2
    right: int = 2

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("left и right должны быть >= 1")

    def detect(self, df: pd.DataFrame) -> list[SwingPoint]:
        if df is None or df.empty:
            return []

        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        n = len(high)

        points: list[SwingPoint] = []
        for i in range(self.left, n - self.right):
            if high[i] == high[i - self.left : i + self.right + 1].max():
                points.append(SwingPoint(index=i, price=float(high[i]), kind=SwingKind.HIGH))
            if low[i] == low[i - self.left : i + self.right + 1].min():
                points.append(SwingPoint(index=i, price=float(low[i]), kind=SwingKind.LOW))
        points.sort(key=lambda p: p.index)
        return points
