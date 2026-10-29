"""Векторный поиск уровней S/R совпадает с прежним поэлементным алгоритмом."""

import numpy as np
import pandas as pd
import pytest

from src.market_context.models import SRType
from src.market_context.sr_levels import SRLevelsCalculator


def _legacy_pivots(calc: SRLevelsCalculator, df: pd.DataFrame) -> list[dict]:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(high)
    w = calc._fractal_side
    pivots: list[dict] = []
    for i in range(w, n - w):
        h_window = high[i - w : i + w + 1]
        l_window = low[i - w : i + w + 1]
        if high[i] == h_window.max() and np.sum(h_window == high[i]) == 1:
            pivots.append({"price": high[i], "kind": SRType.RESISTANCE})
        if low[i] == l_window.min() and np.sum(l_window == low[i]) == 1:
            pivots.append({"price": low[i], "kind": SRType.SUPPORT})
    return pivots


def _legacy_cluster(calc: SRLevelsCalculator, pivots: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for p in pivots:
        merged = False
        for c in clusters:
            if calc._bucket_close(c["price"], p["price"]):
                c["prices"].append(p["price"])
                c["touches"] += 1
                c["kinds"].append(p["kind"])
                counts = {
                    SRType.SUPPORT: c["kinds"].count(SRType.SUPPORT),
                    SRType.RESISTANCE: c["kinds"].count(SRType.RESISTANCE),
                }
                c["kind"] = max(counts, key=counts.get)
                merged = True
                break
        if not merged:
            clusters.append({"prices": [p["price"]], "price": p["price"], "touches": 1,
                             "kinds": [p["kind"]], "kind": p["kind"]})
    for c in clusters:
        c["price"] = float(np.mean(c["prices"]))
    return clusters


def _frame(n: int, seed: int, step: float | None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 70 + np.cumsum(rng.normal(0, 0.05, n))
    high = close + rng.uniform(0, 0.08, n)
    low = close - rng.uniform(0, 0.08, n)
    if step is not None:
        # Реальные цены лежат на сетке шага: много равных экстремумов и повторов.
        high, low = np.round(high / step) * step, np.round(low / step) * step
    return pd.DataFrame({"high": high, "low": low, "close": close})


CASES = [(n, seed, step) for n in (0, 3, 5, 6, 50, 3000) for seed in (1, 2) for step in (None, 0.01, 0.1)]


@pytest.mark.parametrize("n,seed,step", CASES)
@pytest.mark.parametrize("fractal_bars", [3, 5, 7])
def test_pivots_and_clusters_match_legacy(n, seed, step, fractal_bars):
    calc = SRLevelsCalculator(fractal_bars=fractal_bars)
    df = _frame(n, seed, step)

    pivots = calc._find_pivots(df)
    legacy = _legacy_pivots(calc, df)
    assert [(p["price"], p["kind"]) for p in pivots] == [(p["price"], p["kind"]) for p in legacy]

    clusters = calc._cluster(pivots)
    expected = _legacy_cluster(calc, legacy)
    assert [(c["price"], c["touches"], c["kind"]) for c in clusters] == [
        (c["price"], c["touches"], c["kind"]) for c in expected
    ]


def test_nan_bars_are_never_pivots():
    calc = SRLevelsCalculator()
    df = _frame(200, 3, 0.01)
    df.loc[[10, 11, 50, 120], ["high", "low"]] = np.nan

    assert [(p["price"], p["kind"]) for p in calc._find_pivots(df)] == [
        (p["price"], p["kind"]) for p in _legacy_pivots(calc, df)
    ]


def test_cluster_kind_tie_prefers_support():
    calc = SRLevelsCalculator()
    pivots = [{"price": 100.0, "kind": SRType.RESISTANCE}, {"price": 100.1, "kind": SRType.SUPPORT}]

    assert calc._cluster(pivots)[0]["kind"] is SRType.SUPPORT
    assert _legacy_cluster(calc, pivots)[0]["kind"] is SRType.SUPPORT
