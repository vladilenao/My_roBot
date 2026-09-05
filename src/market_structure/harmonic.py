"""Детектор гармонической формации AB=CD (стратегия 0.2).

Формация 0.2 — геометрическая фибо-конструкция XABCD:
  - B  — ретрейсмент волны XA в интервале [38.2%, 61.8%];
  - |AB| <= 61.8% * |XA|;
  - C  — ретрейсмент хода от A к B в интервале [38.2%, 78.6%];
  - D  — расширение 161.8% волны XA (цель).

Бычья формация: X — вершина, A — впадина, движение A→B вверх, B→C вниз,
цель D выше цены. Медвежья формация — зеркально (X — впадина, D ниже цены).

Класс детектора чисто геометрический: не импортирует стратегии и индикаторы.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from src.market_structure.fibonacci import retracement_level
from src.market_structure.swings import SwingDetector, SwingKind, SwingPoint

# Пропорции стратегии 0.2
B_MIN = 0.382
B_MAX = 0.618
AB_MAX = 0.618
C_MIN = 0.382
C_MAX = 0.786
D_RATIO = 1.618
# Прочие ретрейсменты, используемые как ориентиры для зеркальных формул
RETR_B_C = 0.786


class Direction(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class XabcdPattern:
    """Подтверждённая формация XABCD: развороты X,A,B,C и цель D."""

    x: SwingPoint
    a: SwingPoint
    b: SwingPoint
    c: SwingPoint
    d_target: float
    direction: Direction


@dataclass(frozen=True)
class HarmonicPatternDetector:
    """Детектор формации AB=CD (стратегия 0.2) по разворотам рынка."""

    left: int = 2
    right: int = 2
    pattern: str = "ab_cd_0.2"
    fib_tolerance: float = 0.02

    @property
    def warmup(self) -> int:
        # окно свингов слева/справа + запас на развитие атомов формации
        return 40

    def analyze(self, df: pd.DataFrame) -> list[XabcdPattern]:
        if df is None or df.empty:
            return []
        detector = SwingDetector(left=self.left, right=self.right)
        swings = detector.detect(df)
        if len(swings) < 4:
            return []

        patterns: list[XabcdPattern] = []
        for i in range(len(swings) - 3):
            x = swings[i]
            a = swings[i + 1]
            b = swings[i + 2]
            c = swings[i + 3]
            pattern = self._validate(x, a, b, c)
            if pattern is not None:
                patterns.append(pattern)
        return patterns

    def _validate(
        self, x: SwingPoint, a: SwingPoint, b: SwingPoint, c: SwingPoint
    ) -> XabcdPattern | None:
        # Последовательность разворотов чередуется (X⃗ A⃗ B⃗ C).
        if not (x.index < a.index < b.index < c.index):
            return None

        # Бычья формация: X — вершина, A — впадина, B — вершина, C — впадина.
        if (
            x.kind is SwingKind.HIGH
            and a.kind is SwingKind.LOW
            and b.kind is SwingKind.HIGH
            and c.kind is SwingKind.LOW
        ):
            if self._valid_bull(x, a, b, c):
                return XabcdPattern(
                    x=x, a=a, b=b, c=c,
                    d_target=float(a.price + D_RATIO * (x.price - a.price)),
                    direction=Direction.LONG,
                )
            return None

        # Медвежья формация: X — впадина, A — вершина, B — впадина, C — вершина.
        if (
            x.kind is SwingKind.LOW
            and a.kind is SwingKind.HIGH
            and b.kind is SwingKind.LOW
            and c.kind is SwingKind.HIGH
        ):
            if self._valid_bear(x, a, b, c):
                return XabcdPattern(
                    x=x, a=a, b=b, c=c,
                    d_target=float(a.price - D_RATIO * (a.price - x.price)),
                    direction=Direction.SHORT,
                )
            return None

        return None

    # ── бычья (нисходящая волна XA, лонг) ──────────────────────────────

    def _valid_bull(self, x, a, b, c) -> bool:
        amp_xa = x.price - a.price
        if amp_xa <= 0:
            return False

        # B — ретрейсмент XA в [38.2, 61.8]% с допуском
        low = retracement_level(a.price, x.price, B_MIN)
        high = retracement_level(a.price, x.price, B_MAX)
        if not (low - self.fib_tolerance * amp_xa <= b.price <= high + self.fib_tolerance * amp_xa):
            return False

        # |AB| <= 61.8% |XA|
        ab = b.price - a.price
        if ab <= 0 or ab > AB_MAX * amp_xa * (1 + self.fib_tolerance):
            return False

        # C — ретрейсмент хода A→B в [38.2, 78.6]% c допуском.
        # Ретрейсмент BC измеряется от точки B (стандарт AB=CD):
        # c = b - ratio*(b - a), interval [b - 0.786*ab, b - 0.382*ab].
        amp_ab = b.price - a.price
        low_c = b.price - C_MAX * amp_ab
        high_c = b.price - C_MIN * amp_ab
        if not (low_c - self.fib_tolerance * amp_ab <= c.price <= high_c + self.fib_tolerance * amp_ab):
            return False

        # Цель D выше цены подтверждения C (вход в отрезке C→D)
        d_target = a.price + D_RATIO * amp_xa
        return d_target > c.price

    # ── медвежья (восходящая волна XA, шорт) ───────────────────────────

    def _valid_bear(self, x, a, b, c) -> bool:
        amp_ax = a.price - x.price
        if amp_ax <= 0:
            return False

        # B — ретрейсмент восходящей волны XA в [38.2, 61.8]%: B ниже A.
        low = retracement_level(a.price, x.price, B_MAX)  # retr(0.618) — дальше от A
        high = retracement_level(a.price, x.price, B_MIN)  # retr(0.382) — ближе к A
        # low <= B <= high
        if not (low - self.fib_tolerance * amp_ax <= b.price <= high + self.fib_tolerance * amp_ax):
            return False

        # |AB| <= 61.8% |XA| (A->B вниз)
        ab = a.price - b.price
        if ab <= 0 or ab > AB_MAX * amp_ax * (1 + self.fib_tolerance):
            return False

        # C — ретрейсмент хода A→B в [38.2, 78.6]%: C выше B.
        amp_ab = a.price - b.price
        low_c = retracement_level(b.price, a.price, C_MIN)
        high_c = retracement_level(b.price, a.price, C_MAX)
        if not (low_c - self.fib_tolerance * amp_ab <= c.price <= high_c + self.fib_tolerance * amp_ab):
            return False

        # Цель D ниже цены подтверждения C (вход в отрезке C→D вниз)
        d_target = a.price - D_RATIO * amp_ax
        return d_target < c.price
