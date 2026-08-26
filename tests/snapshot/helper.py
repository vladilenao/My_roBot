import math
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from src.strategies.strategy import StrategyConfig

DATA_DIR = Path(__file__).resolve().parent / "data"
RTOL = 1e-9

STRATEGY_COLUMNS = {
    "macd_rsi_stoch": {
        "event": ["datetime", "signal", "price", "macd_sum", "rsi_sum", "stoch_sum"],
        "float": ["price", "macd_sum", "rsi_sum", "stoch_sum"],
    },
    "flat_triangle": {
        "event": [
            "datetime", "signal", "close",
            "bbl_20_2.0", "bbu_20_2.0",
            "rsi", "stochk_5_3_3", "stochd_5_3_3",
        ],
        "float": [
            "close", "bbl_20_2.0", "bbu_20_2.0",
            "rsi", "stochk_5_3_3", "stochd_5_3_3",
        ],
    },
}


# ── expected_events ──────────────────────────────────────────

def _macd_rsi_stoch_expected_events(ta: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    signal_columns = config.signal_columns
    strategy_window = config.strategy_window
    sum_columns = [f"{col}_sum" for col in signal_columns]
    output_columns = [col.replace("_signal", "") for col in sum_columns]

    sums = ta[signal_columns].rolling(strategy_window).sum()
    sums.columns = sum_columns

    events = pd.DataFrame(
        {
            "datetime": ta["datetime"],
            "price": ta["close"],
            **{col: sums[col] for col in sum_columns},
        }
    ).dropna(subset=sum_columns)

    buy_mask = pd.Series(True, index=events.index)
    sell_mask = pd.Series(True, index=events.index)
    for col in sum_columns:
        buy_mask &= events[col] > 0
        sell_mask &= events[col] < 0

    events = events[buy_mask | sell_mask].copy()
    events["signal"] = pd.Series(
        np.where(buy_mask[buy_mask | sell_mask], "BUY", "SELL"),
        index=events.index,
        dtype="string",
    )

    rename_map = dict(zip(sum_columns, output_columns))
    events = events.rename(columns=rename_map)

    result_columns = ["datetime", "signal", "price"] + output_columns
    return events[result_columns].reset_index(drop=True)


def _flat_triangle_expected_events(ta: pd.DataFrame) -> pd.DataFrame:
    bb_lower_col = "bbl_20_2.0"
    bb_upper_col = "bbu_20_2.0"
    stoch_k_col = "stochk_5_3_3"
    stoch_d_col = "stochd_5_3_3"

    buy_mask = (
        (ta["close"] <= ta[bb_lower_col])
        & (ta["rsi"] <= 30)
        & (ta[stoch_k_col] < 20)
        & (ta[stoch_k_col] > ta[stoch_d_col])
        & (ta[stoch_k_col].shift(1) <= ta[stoch_d_col].shift(1))
    )

    sell_mask = (
        (ta["close"] >= ta[bb_upper_col])
        & (ta["rsi"] >= 70)
        & (ta[stoch_k_col] > 80)
        & (ta[stoch_k_col] < ta[stoch_d_col])
        & (ta[stoch_k_col].shift(1) >= ta[stoch_d_col].shift(1))
    )

    events = ta[buy_mask | sell_mask].copy()
    events["signal"] = np.where(
        buy_mask[buy_mask | sell_mask], "BUY", "SELL"
    )
    events["signal"] = events["signal"].astype("string")

    result_columns = [
        "datetime", "signal", "close",
        bb_lower_col, bb_upper_col,
        "rsi", stoch_k_col, stoch_d_col,
    ]
    return events[result_columns].reset_index(drop=True)


def expected_events(strategy_name: str, ta: pd.DataFrame, config: StrategyConfig | None = None) -> pd.DataFrame:
    if strategy_name == "macd_rsi_stoch":
        return _macd_rsi_stoch_expected_events(ta, config)
    if strategy_name == "flat_triangle":
        return _flat_triangle_expected_events(ta)
    raise ValueError(f"Неизвестная стратегия: {strategy_name}")


# ── helpers ──────────────────────────────────────────────────

def expected_filename(strategy_name):
    return f"{strategy_name}_expected_signals.csv"


def load_candles_fixture(case):
    df = pd.read_csv(DATA_DIR / case / "candles.csv")
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.as_unit("ns")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype("float64")
    return df


def load_expected(case, strategy_name):
    cols = STRATEGY_COLUMNS[strategy_name]
    expected = pd.read_csv(DATA_DIR / case / expected_filename(strategy_name))
    expected["datetime"] = pd.to_datetime(expected["datetime"]).dt.as_unit("ns")
    for col in cols["float"]:
        expected[col] = expected[col].astype("float64")
    expected["signal"] = expected["signal"].astype("string")
    return expected[cols["event"]]


def compare_events(actual, expected):
    assert_frame_equal(actual, expected, check_exact=False, rtol=RTOL)


def first_divergence(actual, expected):
    float_cols = list(actual.columns.difference(["datetime", "signal"]))
    n = max(len(actual), len(expected))

    def close(x, y):
        return math.isclose(float(x), float(float(y)), rel_tol=RTOL)

    for i in range(n):
        if i >= len(actual):
            return i, None, expected.iloc[i].to_dict()
        if i >= len(expected):
            return i, actual.iloc[i].to_dict(), None
        a, e = actual.iloc[i], expected.iloc[i]
        if (
            a["datetime"] != e["datetime"]
            or a["signal"] != e["signal"]
            or not all(close(a[c], e[c]) for c in float_cols)
        ):
            return i, a.to_dict(), e.to_dict()
    return None, None, None


def write_expected(case, strategy_name, events):
    cols = STRATEGY_COLUMNS[strategy_name]
    events[cols["event"]].to_csv(
        DATA_DIR / case / expected_filename(strategy_name), index=False
    )
