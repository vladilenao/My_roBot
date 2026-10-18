from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.market_context.models import SRLevel, SRType
from src.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SRLevelsCalculator:
    """Статистически чистый (stateless) калькулятор горизонтальных уровней S/R.

    Уровни определяются фрактальными разворотами свечей (пик/впадина в окне) и
    группируются по ценовой близости. Каждому уровню присваивается тип
    (support/resistance) и сила (количество касаний).
    """

    fractal_bars: int = 5
    min_touches: int = 2
    max_levels: int = 6
    price_bucket_pct: float = 0.5

    def __post_init__(self) -> None:
        if self.fractal_bars < 3 or self.fractal_bars % 2 == 0:
            raise ValueError("fractal_bars должен быть нечётным и >= 3")
        if self.min_touches <= 0:
            raise ValueError("min_touches должен быть > 0")
        if self.max_levels <= 0:
            raise ValueError("max_levels должен быть > 0")
        if self.price_bucket_pct <= 0:
            raise ValueError("price_bucket_pct должен быть > 0")

    @property
    def _fractal_side(self) -> int:
        return (self.fractal_bars - 1) // 2

    def compute(self, df: pd.DataFrame, price: float | None = None) -> list[SRLevel]:
        if df is None or df.empty:
            return []

        current_price = price if price is not None else float(df["close"].iloc[-1])
        pivots = self._find_pivots(df)
        if not pivots:
            return []

        clustered = self._cluster(pivots)
        confirmed = [c for c in clustered if c["touches"] >= self.min_touches]
        if not confirmed:
            return []

        confirmed.sort(key=lambda c: abs(c["price"] - current_price))
        selected = confirmed[: self.max_levels]
        selected.sort(key=lambda c: c["price"])

        return [self._to_level(c) for c in selected]

    def _find_pivots(self, df: pd.DataFrame) -> list[dict]:
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        w = self._fractal_side
        size = 2 * w + 1
        if len(high) < size:
            return []
        # Окна [i-w, i+w] для каждого центра i; NaN не даёт разворота, как и раньше.
        high_windows = np.lib.stride_tricks.sliding_window_view(high, size)
        low_windows = np.lib.stride_tricks.sliding_window_view(low, size)
        high_center = high[w : len(high) - w]
        low_center = low[w : len(low) - w]
        resistance = (high_center == high_windows.max(axis=1)) & (
            (high_windows == high_center[:, None]).sum(axis=1) == 1
        )
        support = (low_center == low_windows.min(axis=1)) & (
            (low_windows == low_center[:, None]).sum(axis=1) == 1
        )
        pivots: list[dict] = []
        for offset in np.flatnonzero(resistance | support):
            if resistance[offset]:
                pivots.append({"price": high_center[offset], "kind": SRType.RESISTANCE})
            if support[offset]:
                pivots.append({"price": low_center[offset], "kind": SRType.SUPPORT})
        return pivots

    def _cluster(self, pivots: list[dict]) -> list[dict]:
        clusters: list[dict] = []
        # Опорная цена кластера не меняется до конца прохода, а новые кластеры
        # добавляются в конец, поэтому первый подходящий кластер для цены
        # однажды найден навсегда. Цены фьючерсов лежат на сетке шага, так что
        # повторы частые и полный перебор кластеров идёт только для новых цен.
        first_match: dict[float, int] = {}
        for p in pivots:
            price = p["price"]
            index = first_match.get(price)
            if index is None:
                index = next(
                    (i for i, c in enumerate(clusters) if self._bucket_close(c["price"], price)),
                    len(clusters),
                )
                first_match[price] = index
            if index == len(clusters):
                clusters.append(
                    {
                        "prices": [price],
                        "price": price,
                        "touches": 1,
                        "support": 0,
                        "resistance": 0,
                        "kind": p["kind"],
                    }
                )
                self._count_kind(clusters[index], p["kind"])
                continue
            c = clusters[index]
            c["prices"].append(price)
            c["touches"] += 1
            self._count_kind(c, p["kind"])
            # При равенстве побеждает поддержка — порядок прежнего max() по словарю.
            c["kind"] = SRType.SUPPORT if c["support"] >= c["resistance"] else SRType.RESISTANCE
        for c in clusters:
            c["price"] = float(np.mean(c["prices"]))
        return clusters

    @staticmethod
    def _count_kind(cluster: dict, kind: SRType) -> None:
        cluster["support" if kind is SRType.SUPPORT else "resistance"] += 1

    def _bucket_close(self, a: float, b: float) -> bool:
        span = max(abs(a), abs(b))
        if span == 0:
            return a == b
        return abs(a - b) / span * 100.0 <= self.price_bucket_pct

    def _to_level(self, cluster: dict) -> SRLevel:
        sr_type = cluster["kind"]
        label = "support" if sr_type is SRType.SUPPORT else "resistance"
        return SRLevel(
            price=float(cluster["price"]),
            sr_type=sr_type,
            strength=int(cluster["touches"]),
            label=label,
        )
