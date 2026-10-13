"""Приложение использует именованную таблицу, а не неполный SDK helper."""

import pytest
from t_tech.invest import CandleInterval, SubscriptionInterval

from src.api.client import NATIVE_CANDLE_INTERVALS, native_candle_interval
from src.config import TIMEFRAMES


@pytest.mark.parametrize("timeframe,rpc,stream", [
    ("1m", "CANDLE_INTERVAL_1_MIN", "SUBSCRIPTION_INTERVAL_ONE_MINUTE"),
    ("5m", "CANDLE_INTERVAL_5_MIN", "SUBSCRIPTION_INTERVAL_FIVE_MINUTES"),
    ("15m", "CANDLE_INTERVAL_15_MIN", "SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES"),
    ("30m", "CANDLE_INTERVAL_30_MIN", "SUBSCRIPTION_INTERVAL_30_MIN"),
    ("1h", "CANDLE_INTERVAL_HOUR", "SUBSCRIPTION_INTERVAL_ONE_HOUR"),
    ("4h", "CANDLE_INTERVAL_4_HOUR", "SUBSCRIPTION_INTERVAL_4_HOUR"),
    ("1d", "CANDLE_INTERVAL_DAY", "SUBSCRIPTION_INTERVAL_ONE_DAY"),
    ("1w", "CANDLE_INTERVAL_WEEK", "SUBSCRIPTION_INTERVAL_WEEK"),
    ("1M", "CANDLE_INTERVAL_MONTH", "SUBSCRIPTION_INTERVAL_MONTH"),
])
def test_native_pair_uses_explicit_enum_names(timeframe, rpc, stream):
    pair = native_candle_interval(timeframe)
    assert pair.rpc is getattr(CandleInterval, rpc)
    assert pair.stream is getattr(SubscriptionInterval, stream)
    assert pair.rpc is TIMEFRAMES[timeframe]


def test_every_application_timeframe_has_a_native_contract():
    assert set(NATIVE_CANDLE_INTERVALS) == set(TIMEFRAMES)


@pytest.mark.parametrize("timeframe", ["2m", "2h", "unknown", "", "1mo"])
def test_unknown_application_interval_is_not_implicitly_aggregated(timeframe):
    with pytest.raises(ValueError, match="Нет нативного контракта"):
        native_candle_interval(timeframe)


def test_mapping_cannot_be_mutated_by_a_consumer():
    with pytest.raises(TypeError):
        NATIVE_CANDLE_INTERVALS["15m"] = native_candle_interval("1m")
