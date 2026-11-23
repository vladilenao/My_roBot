from datetime import datetime, timezone

import pandas as pd

import pytest

from src.data.timeutil import to_aware_utc, to_aware_utc_series, to_naive


def test_naive_stays_naive():
    value = datetime(2024, 1, 1, 10, 0, 0)
    result = to_naive(value)
    assert result.tzinfo is None
    assert result == value


def test_aware_becomes_naive():
    value = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    result = to_naive(value)
    assert result.tzinfo is None
    assert result == datetime(2024, 1, 1, 10, 0, 0)


def test_pandas_timestamp_naive():
    result = to_naive(pd.Timestamp("2024-01-01 10:00:00"))
    assert result.tzinfo is None


def test_aware_pandas_timestamp_becomes_naive():
    result = to_naive(pd.Timestamp("2024-01-01 10:00:00", tz="UTC"))
    assert result.tzinfo is None
    assert result == pd.Timestamp("2024-01-01 10:00:00")


def test_to_aware_utc_naive_interpreted_as_utc():
    result = to_aware_utc(datetime(2024, 1, 1, 10, 0, 0))
    assert result.tzinfo is not None
    assert result.tzinfo == timezone.utc
    assert result == pd.Timestamp("2024-01-01 10:00:00", tz="UTC")


def test_to_aware_utc_aware_converted_to_utc():
    result = to_aware_utc(pd.Timestamp("2024-01-01 12:00:00", tz="Europe/Moscow"))
    assert result == pd.Timestamp("2024-01-01 09:00:00", tz="UTC")


@pytest.mark.parametrize(
    "values",
    [
        pd.Series(pd.date_range("2026-03-01 06:59", periods=4, freq="min"), index=[10, 11, 12, 13]),
        pd.Series(pd.date_range("2026-03-01 09:59", periods=4, freq="min", tz="Europe/Moscow")),
        pd.Series(pd.date_range("2026-03-01", periods=3, freq="min").astype("datetime64[us]")),
        pd.Series([datetime(2026, 3, 1, 7, 0), pd.Timestamp("2026-03-01 10:01", tz="Europe/Moscow")], dtype=object),
        pd.Series([], dtype="datetime64[ns]"),
    ],
    ids=["naive", "moscow", "microseconds", "mixed-object", "empty"],
)
def test_to_aware_utc_series_matches_elementwise_conversion(values):
    expected = values.map(to_aware_utc)
    result = to_aware_utc_series(values)
    assert list(result.index) == list(expected.index)
    assert list(result) == list(expected)
    assert all(stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0 for stamp in result)
    cutoff = datetime(2026, 3, 1, 7, 0, tzinfo=timezone.utc)
    assert list(result >= cutoff) == [stamp >= cutoff for stamp in expected]
