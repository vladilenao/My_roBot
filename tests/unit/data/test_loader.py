from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.data.loader import load_candles


def make_candle(hours, o=1, h=2, l=0, c=3, volume=10, nano=500000000):
    price = lambda units: SimpleNamespace(units=units, nano=nano)
    return SimpleNamespace(
        time=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hours),
        open=price(o),
        high=price(h),
        low=price(l),
        close=price(c),
        volume=volume,
    )


@pytest.fixture
def api_mocks():
    candles = [make_candle(0), make_candle(1)]
    with patch("src.data.loader.Client") as mock_client_cls, \
         patch("src.data.loader.find_working_instrument", return_value="uid-123") as mock_find, \
         patch("src.data.loader.api_call_with_retry", return_value=candles) as mock_retry:
        yield SimpleNamespace(
            client_cls=mock_client_cls,
            client=mock_client_cls.return_value.__enter__.return_value,
            find=mock_find,
            retry=mock_retry,
            candles=candles,
        )


class TestValidation:
    def test_invalid_timeframe_raises(self, api_mocks):
        with pytest.raises(ValueError, match="Неподдерживаемый таймфрейм"):
            load_candles("NGU6", "future", "2h")

    def test_start_after_end_raises(self, api_mocks):
        with pytest.raises(ValueError, match="меньше даты окончания"):
            load_candles("NGU6", "future", "1h", "2024-02-01", "2024-01-01")

    def test_start_equals_end_raises(self, api_mocks):
        with pytest.raises(ValueError, match="меньше даты окончания"):
            load_candles("NGU6", "future", "1h", "2024-01-01", "2024-01-01")


class TestDateParsing:
    def test_string_dates_parsed_and_forwarded(self, api_mocks):
        load_candles("NGU6", "future", "1h", "2024-01-01", "2024-02-01")

        kwargs = api_mocks.retry.call_args.kwargs
        assert kwargs["from_"] == pd.Timestamp("2024-01-01", tz="UTC")
        assert kwargs["to"] == pd.Timestamp("2024-02-01", tz="UTC")
        assert kwargs["from_"].tzinfo is not None
        assert kwargs["to"].tzinfo is not None

    def test_default_range_is_30_days(self, api_mocks):
        fixed_now = datetime(2024, 6, 1, 12, 0, 0)
        with patch("src.data.loader.now", return_value=fixed_now):
            load_candles("NGU6", "future", "1h")

        kwargs = api_mocks.retry.call_args.kwargs
        assert kwargs["to"] == pd.Timestamp(fixed_now, tz="UTC")
        assert kwargs["from_"] == pd.Timestamp(fixed_now - timedelta(days=30), tz="UTC")


class TestNaiveBoundaries:
    def test_aware_end_naive_start_no_crash(self, api_mocks):
        start = datetime(2024, 1, 1)
        end = datetime(2024, 2, 1, tzinfo=timezone.utc)
        load_candles("NGU6", "future", "1h", start, end)

        kwargs = api_mocks.retry.call_args.kwargs
        assert kwargs["from_"].tzinfo is not None
        assert kwargs["to"].tzinfo is not None
        assert kwargs["from_"] == pd.Timestamp("2024-01-01", tz="UTC")
        assert kwargs["to"] == pd.Timestamp("2024-02-01", tz="UTC")

    def test_naive_end_aware_start_no_crash(self, api_mocks):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 2, 1)
        load_candles("NGU6", "future", "1h", start, end)

        kwargs = api_mocks.retry.call_args.kwargs
        assert kwargs["from_"].tzinfo is not None
        assert kwargs["to"].tzinfo is not None
        assert kwargs["from_"] == pd.Timestamp("2024-01-01", tz="UTC")
        assert kwargs["to"] == pd.Timestamp("2024-02-01", tz="UTC")


class TestRequestContract:
    def test_instrument_id_resolved_and_used(self, api_mocks):
        load_candles("NGU6", "future", "1h")

        api_mocks.find.assert_called_once()
        assert api_mocks.retry.call_args.kwargs["instrument_id"] == "uid-123"

    def test_timeframe_mapped_to_interval(self, api_mocks):
        from src.config import TIMEFRAMES

        load_candles("NGU6", "future", "1h")

        assert api_mocks.retry.call_args.kwargs["interval"] == TIMEFRAMES["1h"]

    def test_retry_wraps_get_all_candles(self, api_mocks):
        load_candles("NGU6", "future", "1h")

        assert api_mocks.retry.call_args.args[0] is api_mocks.client.get_all_candles

    def test_client_receives_token_and_closes(self, api_mocks):
        load_candles("NGU6", "future", "1h", token="tok-xyz")

        api_mocks.client_cls.assert_called_once_with("tok-xyz")
        assert api_mocks.client_cls.return_value.__exit__.called


class TestConversion:
    def test_price_units_nano_to_float(self, api_mocks):
        df, uid = load_candles("NGU6", "future", "1h")

        assert uid == "uid-123"
        assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume"]
        assert df["open"].tolist() == [1.5, 1.5]
        assert df["high"].tolist() == [2.5, 2.5]
        assert df["low"].tolist() == [0.5, 0.5]
        assert df["close"].tolist() == [3.5, 3.5]
        assert df["volume"].tolist() == [10, 10]

    def test_datetime_timezone_removed(self, api_mocks):
        df, _ = load_candles("NGU6", "future", "1h")

        assert df["datetime"].dt.tz is None
        assert df["datetime"].tolist() == [
            datetime(2024, 1, 1, 0),
            datetime(2024, 1, 1, 1),
        ]

    def test_empty_result_returns_empty_dataframe_with_uid(self, api_mocks):
        api_mocks.retry.return_value = []

        df, uid = load_candles("NGU6", "future", "1h")

        assert uid == "uid-123"
        assert isinstance(df, pd.DataFrame)
        assert df.empty
