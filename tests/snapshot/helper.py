import math
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

DATA_DIR = Path(__file__).resolve().parent / "data"
EVENT_COLUMNS = ["datetime", "signal", "price", "macd_sum", "rsi_sum", "stoch_sum"]
FLOAT_COLUMNS = ["price", "macd_sum", "rsi_sum", "stoch_sum"]
RTOL = 1e-9


def expected_filename(strategy_name):
    return f"{strategy_name}_expected_signals.csv"


def load_candles_fixture(case):
    df = pd.read_csv(DATA_DIR / case / "candles.csv")
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.as_unit("ns")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype("float64")
    return df


def load_expected(case, strategy_name):
    expected = pd.read_csv(DATA_DIR / case / expected_filename(strategy_name))
    expected["datetime"] = pd.to_datetime(expected["datetime"]).dt.as_unit("ns")
    for col in FLOAT_COLUMNS:
        expected[col] = expected[col].astype("float64")
    expected["signal"] = expected["signal"].astype("string")
    return expected[EVENT_COLUMNS]


def compare_events(actual, expected):
    assert_frame_equal(actual, expected, check_exact=False, rtol=RTOL)


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


def write_expected(case, strategy_name, events):
    events[EVENT_COLUMNS].to_csv(
        DATA_DIR / case / expected_filename(strategy_name), index=False
    )
