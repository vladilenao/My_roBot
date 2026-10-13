"""Узкий owned bidi-порт: SDK wire-модели не выходят в торговый контур.

Отдельный модуль нужен для lifetime/cancel блокирующего RPC и ограниченного
request iterator. История сохраняет существующий unary ClientProvider.
Используется generated protobuf *того же* установленного legacy SDK:
сырой stub сохраняет cancel и presence/source enum, теряемые helper-генератором.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from queue import Empty, Full, Queue
from threading import Event, Lock

from t_tech.invest.grpc import marketdata_pb2 as wire
from t_tech.invest.grpc.common_pb2 import PingDelaySettings

from src.api.client import LIVE_CANDLE_SOURCE, NATIVE_CANDLE_INTERVALS, native_candle_interval


@dataclass(frozen=True)
class CandleSubscriptionAck:
    uid: str
    timeframe: str | None
    status: int
    action: int
    waiting_close: bool
    source: int | None
    stream_id: str
    subscription_id: str
    tracking_id: str


@dataclass(frozen=True)
class StreamCandle:
    uid: str
    timeframe: str | None
    open_time: datetime
    last_trade_time: datetime
    prices: tuple[Decimal, Decimal, Decimal, Decimal]
    volume: int
    source: int


@dataclass(frozen=True)
class StreamResponse:
    kind: str
    acknowledgements: tuple[CandleSubscriptionAck, ...] = ()
    candle: StreamCandle | None = None
    server_time: datetime | None = None
    stream_id: str = ""


def candle_subscription_request(pairs, *, unsubscribe=False):
    """Несколько пар — один запрос; ACK/capacity остаются per-pair."""
    instruments = []
    for uid, timeframe in sorted(set(pairs)):
        if not isinstance(uid, str) or not uid:
            raise ValueError("Подписка свечей требует непустой UID")
        instruments.append(wire.CandleInstrument(
            instrument_id=uid, interval=native_candle_interval(timeframe).stream.value,
        ))
    if not instruments:
        raise ValueError("Подписка свечей требует хотя бы одну пару")
    return wire.MarketDataRequest(subscribe_candles_request=wire.SubscribeCandlesRequest(
        subscription_action=(wire.SUBSCRIPTION_ACTION_UNSUBSCRIBE if unsubscribe
                             else wire.SUBSCRIPTION_ACTION_SUBSCRIBE),
        instruments=instruments, waiting_close=True,
        candle_source_type=LIVE_CANDLE_SOURCE.value,
    ))


def decode_stream_response(response) -> StreamResponse:
    """Разбор только присутствующего payload без default-True/enum fallback."""
    kind = response.WhichOneof("payload") or "empty"
    intervals = {pair.stream.value: tf for tf, pair in NATIVE_CANDLE_INTERVALS.items()}
    if kind == "subscribe_candles_response":
        payload = response.subscribe_candles_response
        acks = []
        for pair in payload.candles_subscriptions:
            field = pair.DESCRIPTOR.fields_by_name["candle_source_type"]
            source = pair.candle_source_type
            if field.has_presence and not pair.HasField("candle_source_type"):
                source = None
            acks.append(CandleSubscriptionAck(
                pair.instrument_uid, intervals.get(pair.interval), pair.subscription_status,
                pair.subscription_action, pair.waiting_close, source,
                pair.stream_id, pair.subscription_id, payload.tracking_id,
            ))
        return StreamResponse(kind, acknowledgements=tuple(acks))
    if kind == "candle":
        payload = response.candle
        if not payload.HasField("time") or not payload.HasField("last_trade_ts"):
            raise ValueError("Stream-свеча не содержит обязательных временных фактов")
        def price(value):
            return Decimal(value.units) + Decimal(value.nano) / Decimal(1000000000)
        candle = StreamCandle(
            payload.instrument_uid, intervals.get(payload.interval),
            payload.time.ToDatetime(tzinfo=timezone.utc),
            payload.last_trade_ts.ToDatetime(tzinfo=timezone.utc),
            tuple(price(getattr(payload, key)) for key in ("open", "high", "low", "close")),
            payload.volume, payload.candle_source_type,
        )
        return StreamResponse(kind, candle=candle)
    if kind == "ping":
        return StreamResponse(kind, server_time=response.ping.time.ToDatetime(tzinfo=timezone.utc),
                              stream_id=response.ping.stream_id)
    return StreamResponse(kind)


class StreamControlOverflow(RuntimeError):
    """Управляющий запрос не был отправлен; supervisor должен закрыть handoff."""


class MarketDataStreamSession:
    """Один client/channel, request queue и отменяемый stream-call.

    Создание не открывает сеть. open вызывается ingestion-worker. Ошибки RPC
    при итерации передаются supervisor; этот порт не делает скрытый reconnect.
    """

    def __init__(self, context, *, control_queue_max_messages=128):
        if isinstance(control_queue_max_messages, bool) or not isinstance(control_queue_max_messages, int) or control_queue_max_messages <= 0:
            raise ValueError("control_queue_max_messages должен быть положительным целым")
        self._context = context
        self._requests = Queue(maxsize=control_queue_max_messages)
        self._stopped = Event()
        self._lock = Lock()
        self._call = None
        self._entered = False

    def open(self):
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeError("Stream-session уже закрыта")
            if self._entered:
                return self
            client = self._context.__enter__()
            self._entered = True
            try:
                service = client.market_data_stream
                self._call = service.stub.MarketDataStream(
                    request_iterator=self._request_iterator(), metadata=service.metadata,
                )
            except BaseException:
                self._entered = False
                self._context.__exit__(None, None, None)
                raise
        return self

    def _request_iterator(self):
        while not self._stopped.is_set():
            try:
                request = self._requests.get(timeout=0.1)
            except Empty:
                continue
            if not self._stopped.is_set():
                yield request

    def _send(self, request):
        if self._stopped.is_set():
            raise RuntimeError("Stream-session уже закрыта")
        try:
            self._requests.put_nowait(request)
        except Full:
            raise StreamControlOverflow("Переполнена управляющая очередь stream") from None

    def subscribe(self, pairs):
        self._send(candle_subscription_request(pairs))

    def unsubscribe(self, pairs):
        self._send(candle_subscription_request(pairs, unsubscribe=True))

    def configure_ping(self, interval_ms):
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or not 5000 <= interval_ms <= 180000:
            raise ValueError("ping interval должен быть целым в диапазоне 5000–180000 мс")
        self._send(wire.MarketDataRequest(ping_settings=PingDelaySettings(ping_delay_ms=interval_ms)))

    def subscriptions(self):
        self._send(wire.MarketDataRequest(get_my_subscriptions=wire.GetMySubscriptions()))

    def __iter__(self):
        if self._call is None:
            raise RuntimeError("Stream-session не открыта")
        for response in self._call:
            if self._stopped.is_set():
                return
            yield decode_stream_response(response)

    def close(self):
        # Stop не помещается в заполненную request queue. cancel прерывает
        # блокирующий next(call), channel close страхует освобождение ресурсов.
        self._stopped.set()
        with self._lock:
            if self._call is not None:
                self._call.cancel()
            if self._entered:
                self._entered = False
                self._context.__exit__(None, None, None)
