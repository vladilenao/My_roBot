"""Рекурсия ATR Уайлдера на float совпадает бит в бит с прежней поэлементной."""

import numpy as np
import pandas as pd
import pytest

from src.strategies.indicators.atr import AtrWilderIndicator


def _legacy_atr(df: pd.DataFrame, period: int) -> pd.Series:
    data = df.copy()
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        ((data["high"] - data["low"]), (data["high"] - previous_close).abs(), (data["low"] - previous_close).abs()),
        axis=1,
    ).max(axis=1)
    atr = pd.Series(np.nan, index=data.index, dtype=float)
    if len(data) >= period:
        atr.iloc[period - 1] = true_range.iloc[:period].mean()
        for index in range(period, len(data)):
            atr.iloc[index] = ((period - 1) * atr.iloc[index - 1] + true_range.iloc[index]) / period
    return atr


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 70 + np.cumsum(rng.normal(0, 0.05, n))
    return pd.DataFrame({
        "high": np.round((close + rng.uniform(0, 0.1, n)) / 0.01) * 0.01,
        "low": np.round((close - rng.uniform(0, 0.1, n)) / 0.01) * 0.01,
        "close": close,
    }, index=pd.RangeIndex(100, 100 + n))


@pytest.mark.parametrize("n", [0, 1, 13, 14, 15, 2000])
@pytest.mark.parametrize("period", [1, 3, 14])
def test_matches_legacy_bit_for_bit(n, period):
    df = _frame(n, seed=n + period)

    result = AtrWilderIndicator(period=period).compute(df)["atr_wilder"]
    expected = _legacy_atr(df, period)

    assert result.index.equals(expected.index)
    np.testing.assert_array_equal(result.to_numpy(), expected.to_numpy())


def test_nan_bar_propagates_like_legacy():
    df = _frame(100, seed=7)
    df.loc[150, ["high", "low"]] = np.nan

    result = AtrWilderIndicator(period=14).compute(df)["atr_wilder"]

    np.testing.assert_array_equal(result.to_numpy(), _legacy_atr(df, 14).to_numpy())
