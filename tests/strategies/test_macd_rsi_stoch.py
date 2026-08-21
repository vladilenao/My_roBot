import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.config import SIGNAL_WINDOW
from src.indicators.calculator import tech_analyze

STRATEGY_NAME = "macd_rsi_stoch"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EXPECTED_FILENAME = f"{STRATEGY_NAME}_expected_signals.csv"
EVENT_COLUMNS = ["datetime", "signal", "price", "macd_sum", "rsi_sum", "stoch_sum"]
FLOAT_COLUMNS = ["price", "macd_sum", "rsi_sum", "stoch_sum"]
RTOL = 1e-9

CASES = sorted(p.parent.name for p in DATA_DIR.glob(f"*/{EXPECTED_FILENAME}")) if DATA_DIR.exists() else []


def load_candles_fixture(case):
    df = pd.read_csv(DATA_DIR / case / "candles.csv")
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.as_unit("ns")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype("float64")
    return df


def load_expected(case):
    expected = pd.read_csv(DATA_DIR / case / EXPECTED_FILENAME)
    expected["datetime"] = pd.to_datetime(expected["datetime"]).dt.as_unit("ns")
    for col in FLOAT_COLUMNS:
        expected[col] = expected[col].astype("float64")
    expected["signal"] = expected["signal"].astype("string")
    return expected[EVENT_COLUMNS]


def replay_consensus_events(df):
    data_ta = tech_analyze(df)
    sums = data_ta[["macd_signal", "rsi_signal", "stoch_signal"]].rolling(SIGNAL_WINDOW).sum()
    events = pd.DataFrame(
        {
            "datetime": data_ta["datetime"],
            "price": data_ta["close"],
            "macd_sum": sums["macd_signal"],
            "rsi_sum": sums["rsi_signal"],
            "stoch_sum": sums["stoch_signal"],
        }
    ).dropna(subset=FLOAT_COLUMNS)

    buy = (events["macd_sum"] > 0) & (events["rsi_sum"] > 0) & (events["stoch_sum"] > 0)
    sell = (events["macd_sum"] < 0) & (events["rsi_sum"] < 0) & (events["stoch_sum"] < 0)
    events = events[buy | sell].copy()
    signals = np.where(events["macd_sum"] > 0, "BUY", "SELL")
    events["signal"] = pd.Series(signals, index=events.index, dtype="string")
    return events[EVENT_COLUMNS].reset_index(drop=True)


def first_divergence(actual, expected):
    n = max(len(actual), len(expected))

    def close(x, y):
        return math.isclose(float(x), float(y), rel_tol=RTOL)

    for i in range(n):
        if i >= len(actual):
            return i, None, expected.iloc[i].to_dict()
        if i >= len(expected):
            return i, actual.iloc[i].to_dict(), None
        a, e = actual.iloc[i], expected.iloc[i]
        if (
            a["datetime"] != e["datetime"]
            or a["signal"] != e["signal"]
            or not all(close(a[c], e[c]) for c in FLOAT_COLUMNS)
        ):
            return i, a.to_dict(), e.to_dict()
    return None, None, None


def write_expected(case, events):
    events[EVENT_COLUMNS].to_csv(DATA_DIR / case / EXPECTED_FILENAME, index=False)


@pytest.mark.parametrize("case", CASES)
def test_snapshot(case, request):
    df = load_candles_fixture(case)
    actual = replay_consensus_events(df)

    if request.config.getoption("--update-snapshots"):
        write_expected(case, actual)
        return

    expected = load_expected(case)
    try:
        assert_frame_equal(actual, expected, check_exact=False, rtol=RTOL)
    except AssertionError as err:
        row, a, e = first_divergence(actual, expected)
        pytest.fail(
            f"Кейс '{case}': эталон разошёлся с текущим поведением конвейера.\n"
            f"Первое расхождение - строка {row}:\n"
            f"  фактическое: {a}\n"
            f"  эталонное:   {e}\n"
            f"{err}"
        )
