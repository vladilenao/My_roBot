"""Offline-контракт legacy SDK и его реального protobuf wire-слоя.

Наличие dataclass-поля/enum не доказывает, что Services сможет разобрать
соответствующий ответ. Клиент, канал и запросы брокеру здесь не создаются.
"""

from dataclasses import fields
from datetime import datetime, timezone

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from t_tech.invest import _grpc_helpers, schemas
from t_tech.invest.grpc import marketdata_pb2, common_pb2


INTERVAL_NAMES = (
    ("CANDLE_INTERVAL_1_MIN", "SUBSCRIPTION_INTERVAL_ONE_MINUTE"),
    ("CANDLE_INTERVAL_5_MIN", "SUBSCRIPTION_INTERVAL_FIVE_MINUTES"),
    ("CANDLE_INTERVAL_15_MIN", "SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES"),
    ("CANDLE_INTERVAL_30_MIN", "SUBSCRIPTION_INTERVAL_30_MIN"),
    ("CANDLE_INTERVAL_HOUR", "SUBSCRIPTION_INTERVAL_ONE_HOUR"),
    ("CANDLE_INTERVAL_4_HOUR", "SUBSCRIPTION_INTERVAL_4_HOUR"),
    ("CANDLE_INTERVAL_DAY", "SUBSCRIPTION_INTERVAL_ONE_DAY"),
    ("CANDLE_INTERVAL_WEEK", "SUBSCRIPTION_INTERVAL_WEEK"),
    ("CANDLE_INTERVAL_MONTH", "SUBSCRIPTION_INTERVAL_MONTH"),
)
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def wire_timestamp(when=NOW):
    stamp = Timestamp()
    stamp.FromDatetime(when)
    return stamp


@pytest.mark.parametrize("rpc_name,stream_name", INTERVAL_NAMES)
def test_named_native_intervals_serialize_in_subscribe_array(rpc_name, stream_name):
    rpc_interval = getattr(schemas.CandleInterval, rpc_name)
    interval = getattr(schemas.SubscriptionInterval, stream_name)
    assert marketdata_pb2.CandleInterval.Name(rpc_interval.value) == rpc_name
    request = schemas.MarketDataRequest(
        subscribe_candles_request=schemas.SubscribeCandlesRequest(
            subscription_action=schemas.SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[schemas.CandleInstrument(instrument_id="uid-a", interval=interval)],
            waiting_close=True,
            candle_source_type=schemas.CandleSource.CANDLE_SOURCE_UNSPECIFIED,
        )
    )
    wire = _grpc_helpers.dataclass_to_protobuf(request, marketdata_pb2.MarketDataRequest())
    wire = marketdata_pb2.MarketDataRequest.FromString(wire.SerializeToString())
    assert wire.WhichOneof("payload") == "subscribe_candles_request"
    subscription = wire.subscribe_candles_request
    assert subscription.subscription_action == marketdata_pb2.SUBSCRIPTION_ACTION_SUBSCRIBE
    assert subscription.waiting_close is True
    assert subscription.candle_source_type == 0
    assert subscription.instruments[0].instrument_id == "uid-a"
    assert marketdata_pb2.SubscriptionInterval.Name(subscription.instruments[0].interval) == stream_name


def test_historical_and_stream_closure_and_source_contracts_differ():
    assert "is_complete" not in {field.name for field in fields(schemas.Candle)}
    assert "is_complete" in {field.name for field in fields(schemas.HistoricCandle)}
    quote = common_pb2.Quotation(units=10)
    historic = marketdata_pb2.HistoricCandle(
        open=quote, high=quote, low=quote, close=quote, volume=2,
        time=wire_timestamp(), is_complete=True,
        candle_source=schemas.CandleSource.CANDLE_SOURCE_EXCHANGE.value,
    )
    parsed = _grpc_helpers.protobuf_to_dataclass(historic, schemas.HistoricCandle)
    assert parsed.is_complete is True
    assert parsed.candle_source is schemas.CandleSource.CANDLE_SOURCE_EXCHANGE
    wire = marketdata_pb2.MarketDataResponse(candle=marketdata_pb2.Candle(
        instrument_uid="uid-a", interval=marketdata_pb2.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
        open=quote, high=quote, low=quote, close=quote, volume=2,
        time=wire_timestamp(), last_trade_ts=wire_timestamp(),
        candle_source_type=schemas.CandleSource.CANDLE_SOURCE_EXCHANGE.value,
    ))
    parsed = _grpc_helpers.protobuf_to_dataclass(wire, schemas.MarketDataResponse)
    assert parsed.candle.time == NOW
    assert parsed.candle.instrument_uid == "uid-a"
    assert parsed.candle.candle_source_type is schemas.CandleSource.CANDLE_SOURCE_EXCHANGE
    assert parsed.ping is None
    assert parsed.subscribe_candles_response is None


def test_ack_preserves_uid_interval_action_waiting_close_and_ids():
    wire = marketdata_pb2.MarketDataResponse(
        subscribe_candles_response=marketdata_pb2.SubscribeCandlesResponse(
            tracking_id="tracking-a",
            candles_subscriptions=[marketdata_pb2.CandleSubscription(
                instrument_uid="uid-a", interval=marketdata_pb2.SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES,
                subscription_status=marketdata_pb2.SUBSCRIPTION_STATUS_SUCCESS,
                subscription_action=marketdata_pb2.SUBSCRIPTION_ACTION_SUBSCRIBE,
                waiting_close=True, stream_id="stream-a", subscription_id="subscription-a",
            )],
        )
    )
    assert wire.WhichOneof("payload") == "subscribe_candles_response"
    ack = _grpc_helpers.protobuf_to_dataclass(wire, schemas.MarketDataResponse)
    pair = ack.subscribe_candles_response.candles_subscriptions[0]
    assert pair.instrument_uid == "uid-a"
    assert pair.interval is schemas.SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES
    assert pair.subscription_status is schemas.SubscriptionStatus.SUBSCRIPTION_STATUS_SUCCESS
    assert pair.subscription_action is schemas.SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE
    assert pair.waiting_close is True
    assert (pair.stream_id, pair.subscription_id) == ("stream-a", "subscription-a")
    assert ack.subscribe_candles_response.tracking_id == "tracking-a"
    assert ack.candle is None


def test_ping_settings_is_a_separate_request_payload():
    request = schemas.MarketDataRequest(ping_settings=schemas.PingDelaySettings(ping_delay_ms=15000))
    wire = _grpc_helpers.dataclass_to_protobuf(request, marketdata_pb2.MarketDataRequest())
    assert wire.WhichOneof("payload") == "ping_settings"
    assert wire.ping_settings.ping_delay_ms == 15000
    assert not wire.HasField("subscribe_candles_request")


def test_empty_response_does_not_materialize_a_candle():
    parsed = _grpc_helpers.protobuf_to_dataclass(marketdata_pb2.MarketDataResponse(), schemas.MarketDataResponse)
    assert parsed.candle is None
    assert parsed.ping is None
    assert parsed.subscribe_candles_response is None


def test_server_ping_decodes_timestamp_without_using_it_as_candle_time():
    wire = marketdata_pb2.MarketDataResponse(ping=common_pb2.Ping(time=wire_timestamp(), stream_id="stream-a"))
    parsed = _grpc_helpers.protobuf_to_dataclass(wire, schemas.MarketDataResponse)
    assert parsed.ping.time == NOW
    assert parsed.ping.stream_id == "stream-a"
    assert parsed.candle is None


def test_get_candles_profile_and_limit_serialize_explicitly():
    request = schemas.GetCandlesRequest(
        instrument_id="uid-a", from_=NOW, to=NOW,
        interval=schemas.CandleInterval.CANDLE_INTERVAL_1_MIN,
        candle_source_type=schemas.CandleSource.CANDLE_SOURCE_UNSPECIFIED, limit=2400,
    )
    wire = _grpc_helpers.dataclass_to_protobuf(request, marketdata_pb2.GetCandlesRequest())
    assert wire.instrument_id == "uid-a"
    assert wire.candle_source_type == 0
    assert wire.limit == 2400
