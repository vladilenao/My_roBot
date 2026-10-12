"""Контракт owned stream-порта без сети и денежных компонентов."""

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from t_tech.invest.grpc import marketdata_pb2 as wire

from src.api.client import LIVE_CANDLE_SOURCE, TinkoffClientProvider
from src.api.emulator_client import HistoricalClient
from src.api.market_data_stream import (
    MarketDataStreamSession, StreamControlOverflow,
    candle_subscription_request, decode_stream_response,
)


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "30m", "4h", "1d", "1w"])
def test_subscription_uses_uid_closed_mode_and_compatible_profile(timeframe):
    request = candle_subscription_request([("uid-a", timeframe), ("uid-a", timeframe)])
    pair = request.subscribe_candles_request
    assert len(pair.instruments) == 1
    assert pair.instruments[0].instrument_id == "uid-a"
    assert pair.waiting_close is True
    assert pair.candle_source_type == LIVE_CANDLE_SOURCE.value == 0


def test_batch_does_not_add_duplicate_pairs_and_unsubscribe_keeps_profile():
    request = candle_subscription_request([("u", "1m"), ("u", "15m"), ("u", "1m")], unsubscribe=True)
    pair = request.subscribe_candles_request
    assert len(pair.instruments) == 2
    assert pair.subscription_action == wire.SUBSCRIPTION_ACTION_UNSUBSCRIBE
    assert pair.waiting_close is True
    assert pair.candle_source_type == 0


def test_ack_missing_source_remains_distinct_from_explicit_exchange():
    payload = wire.MarketDataResponse(subscribe_candles_response=wire.SubscribeCandlesResponse(
        candles_subscriptions=[wire.CandleSubscription(instrument_uid="u", interval=1,
            waiting_close=True, subscription_status=1, subscription_action=1)]))
    ack = decode_stream_response(payload).acknowledgements[0]
    assert ack.source is None
    payload.subscribe_candles_response.candles_subscriptions[0].candle_source_type = 1
    assert decode_stream_response(payload).acknowledgements[0].source == 1


def test_unknown_ack_interval_and_source_are_not_cast_to_supported_defaults():
    payload = wire.MarketDataResponse(subscribe_candles_response=wire.SubscribeCandlesResponse(
        candles_subscriptions=[wire.CandleSubscription(instrument_uid="u", interval=99,
            candle_source_type=99, waiting_close=True, subscription_status=1, subscription_action=1)]))
    ack = decode_stream_response(payload).acknowledgements[0]
    assert ack.timeframe is None
    assert ack.source == 99


def test_empty_payload_has_no_candle_or_ack():
    response = decode_stream_response(wire.MarketDataResponse())
    assert response.kind == "empty"
    assert response.candle is None
    assert response.acknowledgements == ()


def test_candle_wire_timestamp_preserves_same_utc_moment_and_last_trade_fact():
    stamp, trade = Timestamp(), Timestamp()
    stamp.FromDatetime(datetime.fromisoformat("2026-10-12T15:00:00+03:00"))
    trade.FromDatetime(datetime.fromisoformat("2026-10-12T12:00:20+00:00"))
    candle = decode_stream_response(wire.MarketDataResponse(candle=wire.Candle(
        instrument_uid="u", interval=1, time=stamp, last_trade_ts=trade,
        candle_source_type=1, volume=1,
    ))).candle
    assert candle.open_time == datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    assert candle.last_trade_time == datetime(2026, 10, 12, 12, 0, 20, tzinfo=timezone.utc)
    assert candle.source == 1


def test_historical_client_has_no_required_stream_port():
    assert not hasattr(HistoricalClient, "stream_session")
    assert callable(TinkoffClientProvider.stream_session)


def test_stream_creation_does_not_enter_context_until_worker_opens():
    entries = []
    call = SimpleNamespace(cancel=lambda: entries.append("cancel"))
    @contextmanager
    def context():
        entries.append("enter")
        try:
            yield SimpleNamespace(market_data_stream=SimpleNamespace(metadata=("metadata",),
                stub=SimpleNamespace(MarketDataStream=lambda **kwargs: call)))
        finally:
            entries.append("exit")
    session = MarketDataStreamSession(context())
    assert entries == []
    session.open()
    assert entries == ["enter"]
    session.close()
    session.close()
    assert entries.count("exit") == 1


def test_stop_cancels_blocked_reader_without_waiting_for_next_trade():
    reading, stopped, exited = Event(), Event(), Event()
    class Call:
        def __iter__(self):
            return self
        def __next__(self):
            reading.set()
            assert stopped.wait(2)
            raise StopIteration
        def cancel(self):
            stopped.set()
    @contextmanager
    def context():
        try:
            yield SimpleNamespace(market_data_stream=SimpleNamespace(metadata=(),
                stub=SimpleNamespace(MarketDataStream=lambda **kwargs: Call())))
        finally:
            exited.set()
    session = MarketDataStreamSession(context()).open()
    thread = Thread(target=lambda: list(session))
    thread.start()
    try:
        assert reading.wait(1)
        session.close()
        thread.join(1)
        assert not thread.is_alive()
        assert exited.is_set()
    finally:
        stopped.set()
        session.close()
        thread.join(2)


def test_full_control_queue_is_visible_and_does_not_block_stop():
    session = MarketDataStreamSession(None, control_queue_max_messages=1)
    session.subscribe([("u", "1m")])
    with pytest.raises(StreamControlOverflow):
        session.configure_ping(15000)
    session.close()
    with pytest.raises(RuntimeError, match="закрыта"):
        session.subscribe([("u", "1m")])


def test_rpc_error_during_iteration_is_not_treated_as_successful_open():
    class Call:
        def __iter__(self):
            raise RuntimeError("UNAVAILABLE")
        def cancel(self):
            pass
    @contextmanager
    def context():
        yield SimpleNamespace(market_data_stream=SimpleNamespace(metadata=(),
            stub=SimpleNamespace(MarketDataStream=lambda **kwargs: Call())))
    session = MarketDataStreamSession(context()).open()
    try:
        with pytest.raises(RuntimeError, match="UNAVAILABLE"):
            list(session)
    finally:
        session.close()
