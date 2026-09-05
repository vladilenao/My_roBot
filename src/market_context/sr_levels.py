from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.market_context.models import SRLevel, SRType


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
        n = len(high)
        w = self._fractal_side
        pivots: list[dict] = []
        for i in range(w, n - w):
            h_window = high[i - w : i + w + 1]
            l_window = low[i - w : i + w + 1]
            if high[i] == h_window.max() and np.sum(h_window == high[i]) == 1:
                pivots.append({"price": high[i], "kind": SRType.RESISTANCE})
            if low[i] == l_window.min() and np.sum(l_window == low[i]) == 1:
                pivots.append({"price": low[i], "kind": SRType.SUPPORT})
        return pivots

    def _cluster(self, pivots: list[dict]) -> list[dict]:
        clusters: list[dict] = []
        for p in pivots:
            merged = False
            for c in clusters:
                if self._bucket_close(c["price"], p["price"]):
                    c["prices"].append(p["price"])
                    c["touches"] += 1
                    c["kinds"].append(p["kind"])
                    self._update_cluster_kind(c)
                    merged = True
                    break
            if not merged:
                clusters.append(
                    {
                        "prices": [p["price"]],
                        "price": p["price"],
                        "touches": 1,
                        "kinds": [p["kind"]],
                        "kind": p["kind"],
                    }
                )
        for c in clusters:
            c["price"] = float(np.mean(c["prices"]))
        return clusters

    def _update_cluster_kind(self, cluster: dict) -> None:
        kind_counts = {
            SRType.SUPPORT: cluster["kinds"].count(SRType.SUPPORT),
            SRType.RESISTANCE: cluster["kinds"].count(SRType.RESISTANCE),
        }
        cluster["kind"] = max(kind_counts, key=kind_counts.get)

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
