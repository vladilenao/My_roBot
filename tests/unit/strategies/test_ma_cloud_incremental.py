"""Продолжение проигрыша ma_cloud_rsi_macd даёт те же решения, что полный проигрыш."""

import logging

import numpy as np
import pandas as pd
import pytest

from src.strategies.contracts import SignalType
from src.strategies.ma_cloud_rsi_macd_strategy import MaCloudRsiMacdStrategy


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 70 + np.cumsum(rng.normal(0, 0.4, n))
    return pd.DataFrame({
        "datetime": pd.date_range("2025-03-10", periods=n, freq="15min"),
        "open": close, "high": close + rng.uniform(0, 0.3, n),
        "low": close - rng.uniform(0, 0.3, n), "close": close, "volume": 100,
    })


def _fresh(ta: pd.DataFrame, timeframe: str = "15m"):
    return MaCloudRsiMacdStrategy().decide(ta, timeframe)


@pytest.fixture(scope="module")
def ta() -> pd.DataFrame:
    return MaCloudRsiMacdStrategy().compute(_frame(260, seed=2))


def test_growing_frame_matches_full_replay_on_every_bar(ta):
    strategy = MaCloudRsiMacdStrategy()
    decisions = []
    for end in range(1, len(ta) + 1):
        prefix = ta.iloc[:end]
        incremental = strategy.decide(prefix, "15m")
        assert incremental == _fresh(prefix), f"расхождение на баре {end - 1}"
        decisions.append(incremental.signal_type)

    # Проверка осмысленна, только если на данных были входы в обе стороны.
    assert SignalType.BUY in decisions and SignalType.SELL in decisions


def test_same_frame_twice_returns_same_decision(ta):
    strategy = MaCloudRsiMacdStrategy()

    assert strategy.decide(ta, "15m") == strategy.decide(ta, "15m") == _fresh(ta)


def test_corrected_history_falls_back_to_full_replay(ta):
    strategy = MaCloudRsiMacdStrategy()
    strategy.decide(ta.iloc[:200], "15m")
    corrected = ta.copy()
    corrected.loc[150:, "rsi_signal"] = 0

    assert strategy.decide(corrected, "15m") == _fresh(corrected)


def test_shifted_frame_start_falls_back_to_full_replay(ta):
    strategy = MaCloudRsiMacdStrategy()
    strategy.decide(ta.iloc[:200], "15m")
    shifted = ta.iloc[30:].reset_index(drop=True)

    assert strategy.decide(shifted, "15m") == _fresh(shifted)


def test_shared_instance_serves_two_instruments_and_timeframes(ta):
    other = MaCloudRsiMacdStrategy().compute(_frame(260, seed=9))
    strategy = MaCloudRsiMacdStrategy()
    for end in range(50, 261, 7):
        for frame, timeframe in ((ta, "15m"), (other, "15m"), (ta, "1h")):
            prefix = frame.iloc[:end]
            assert strategy.decide(prefix, timeframe) == _fresh(prefix, timeframe)


def test_entry_is_logged_once_per_signal_not_per_history_bar(ta, caplog):
    strategy = MaCloudRsiMacdStrategy()
    entries = 0
    with caplog.at_level(logging.INFO, logger="src.strategies.ma_cloud_rsi_macd_strategy"):
        for end in range(1, len(ta) + 1):
            if strategy.decide(ta.iloc[:end], "15m").signal_type is not SignalType.HOLD:
                entries += 1

    logged = [r for r in caplog.records if "third indicator confirmed" in r.getMessage()]
    assert entries > 0
    assert len(logged) == entries
