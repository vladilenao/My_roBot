import numpy as np
import pandas as pd
import pytest

from src.strategies.macd_rsi_stoch.indicators.calculator import tech_analyze


def make_ohlcv(n=60):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    closes = 100 + 8 * np.sin(np.arange(n) / 4) + np.arange(n) * 0.1
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": [1000] * n,
        }
    )


def make_trending_ohlcv():
    flat = [100.0 - i * 0.05 for i in range(40)]
    rise = [flat[-1] * (1.02 ** i) for i in range(1, 9)]
    closes = flat + rise
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        }
    )


REQUIRED_INDICATOR_COLUMNS = [
    "macd_12_26_9",
    "macds_12_26_9",
    "macdh_12_26_9",
    "stochk_14_3_3",
    "stochd_14_3_3",
    "rsi",
]
SIGNAL_COLUMNS = ["rsi_signal", "macd_signal", "stoch_signal"]


class TestStructure:
    def test_input_dataframe_not_mutated(self):
        df = make_ohlcv()
        original_columns = list(df.columns)
        original_values = df["close"].copy()

        tech_analyze(df)

        assert list(df.columns) == original_columns
        assert df["close"].equals(original_values)

    def test_returns_new_frame_same_rows(self):
        df = make_ohlcv()
        result = tech_analyze(df)

        assert result is not df
        assert len(result) == len(df)

    def test_indicator_columns_present(self):
        result = tech_analyze(make_ohlcv())

        missing = [c for c in REQUIRED_INDICATOR_COLUMNS if c not in result.columns]
        assert not missing

    def test_all_columns_lowercase(self):
        result = tech_analyze(make_ohlcv())

        assert all(c == c.lower() for c in result.columns)

    def test_signal_values_in_allowed_set(self):
        result = tech_analyze(make_ohlcv())

        for col in SIGNAL_COLUMNS:
            assert set(result[col].unique()).issubset({-1, 0, 1})

    def test_first_row_signals_are_zero_on_nan_warmup(self):
        result = tech_analyze(make_ohlcv(n=40))

        first = result.iloc[0]
        assert all(first[col] == 0 for col in SIGNAL_COLUMNS)


class TestSignalRulesAgainstSpec:
    @staticmethod
    def expected_rsi_signal(rsi):
        prev = rsi.shift(1)
        return np.where(
            (rsi > 50) & (prev < 50),
            1,
            np.where((rsi < 50) & (prev > 50), -1, 0),
        )

    @staticmethod
    def expected_macd_signal(macd, macds):
        macds_prev = macds.shift(1)
        macd_prev = macd.shift(1)
        bull = (macds > macds_prev) & (macds > macd_prev) & (macd < 0) & (macds < 0)
        bear = (macds < macds_prev) & (macds < macd_prev) & (macd > 0) & (macds > 0)
        return np.where(bull, 1, np.where(bear, -1, 0))

    @staticmethod
    def expected_stoch_signal(stochk):
        k_prev = stochk.shift(1)
        bull = (stochk > k_prev) & (k_prev < 20) & (stochk > 20) & (stochk < 50)
        bear = (stochk < k_prev) & (k_prev > 80) & (stochk < 80) & (stochk > 50)
        return np.where(bull, 1, np.where(bear, -1, 0))

    def test_rsi_rule(self):
        result = tech_analyze(make_ohlcv())

        np.testing.assert_array_equal(
            result["rsi_signal"], self.expected_rsi_signal(result["rsi"])
        )

    def test_macd_rule(self):
        result = tech_analyze(make_ohlcv())

        np.testing.assert_array_equal(
            result["macd_signal"],
            self.expected_macd_signal(result["macd_12_26_9"], result["macds_12_26_9"]),
        )

    def test_stoch_rule(self):
        result = tech_analyze(make_ohlcv())

        np.testing.assert_array_equal(
            result["stoch_signal"],
            self.expected_stoch_signal(result["stochk_14_3_3"]),
        )


class TestEngineeredCrossovers:
    def test_uptrend_produces_rsi_upcross(self):
        result = tech_analyze(make_trending_ohlcv())

        upcross = result[result["rsi_signal"] == 1]
        assert len(upcross) >= 1
        for row_idx in upcross.index:
            assert result.loc[row_idx, "rsi"] > 50
            if row_idx > 0:
                assert result.loc[row_idx - 1, "rsi"] < 50

    def test_no_rsi_signal_without_crossover(self):
        result = tech_analyze(make_trending_ohlcv())
        rsi = result["rsi"]

        last = result.iloc[-1]
        crossed_now = (last["rsi_signal"] != 0) and (
            (rsi.iloc[-2] < 50 < rsi.iloc[-1]) or (rsi.iloc[-2] > 50 > rsi.iloc[-1])
        )
        if not crossed_now and rsi.iloc[-2] > 50 and rsi.iloc[-1] > 50:
            assert last["rsi_signal"] == 0
