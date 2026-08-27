from datetime import datetime, timezone

import pandas as pd

from src.data.timeutil import to_naive, to_aware_utc


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
