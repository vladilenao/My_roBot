"""Фейковый Finam Trade API без сети: сервисы gRPC поверх простого состояния счёта.

Значения секрета, JWT и номера счёта — заведомо фейковые и достаточно длинные,
чтобы тест на утечку искал их во всех файлах.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import grpc

from src.finam.gateway import FinamGateway
from src.finam.proto.google.type import decimal_pb2, interval_pb2, money_pb2
from src.finam.proto.grpc.tradeapi.v1 import side_pb2, trade_pb2
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2 as auth_pb2
from src.finam.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2 as md_pb2
from src.finam.proto.grpc.tradeapi.v1.metrics import usage_metrics_service_pb2 as metrics_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.finam.records import CallRecorder
from src.sandbox.gateway import BrokerGateway
from tests.unit.sandbox.fakes import FakeBroker, FakeClient

SECRET = "fake-finam-secret-0123456789abcdef"
ACCOUNT_ID = "FAKE-FINAM-ACC-778899"
DEMO_ACCOUNT_ID = "FAKE-FINAM-DEMO-112233"
TINKOFF = "fake-tinkoff-token-0123456789"
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
NG = "NGZ6@RTSX"
SBER = "SBER@MISX"
StatusCode = grpc.StatusCode
S = orders_pb2.OrderStatus


def d(value) -> decimal_pb2.Decimal:
    return decimal_pb2.Decimal(value=str(value))


def stamp(moment: datetime):
    from google.protobuf.timestamp_pb2 import Timestamp

    result = Timestamp()
    result.FromDatetime(moment)
    return result


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: StatusCode, details: str = "", trailing=()):
        super().__init__()
        self._code, self._details, self._trailing = code, details, tuple(trailing)

    def code(self):
        return self._code

    def details(self):
        return self._details

    def trailing_metadata(self):
        return self._trailing

    def __str__(self) -> str:  # объект исключения не должен попадать в вывод — тест проверит
        return f"RAW-RPC-ERROR {self._details}"


class FakeStream:
    """Ответ server-stream: итератор с ``cancel``; сообщения кладёт брокер."""

    def __init__(self, broker: "FakeFinam", kind: str, jwt: str):
        self.broker, self.kind, self.jwt = broker, kind, jwt
        self.queue: queue.Queue = queue.Queue()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self.cancelled:
                raise FakeRpcError(StatusCode.CANCELLED, "отменён")
            if self.broker.jwt_expired(self.jwt) and self.broker.streams_die_on_expiry:
                raise FakeRpcError(StatusCode.UNAUTHENTICATED, "token expired")
            try:
                return self.queue.get(timeout=0.01)
            except queue.Empty:
                continue


class RenewalStream:
    """``SubscribeJwtRenewal``: один новый JWT через ``renew_after_min`` минут."""

    def __init__(self, broker: "FakeFinam"):
        self.broker, self.start, self.sent, self.cancelled = broker, broker.now, False, False

    def cancel(self):
        self.cancelled = True

    def __iter__(self):
        return self

    def __next__(self):
        while not self.cancelled:
            if not self.sent and self.broker.now >= self.start + timedelta(minutes=self.broker.renew_after_min):
                self.sent = True
                return auth_pb2.SubscribeJwtRenewalResponse(token=self.broker.issue())
            time.sleep(0.002)
        raise FakeRpcError(StatusCode.CANCELLED, "отменён")


@dataclass
class FakeFinam:
    account_ids: list = field(default_factory=lambda: [ACCOUNT_ID, DEMO_ACCOUNT_ID])
    readonly: bool = False
    now: datetime = NOW
    last: dict = field(default_factory=lambda: {NG: Decimal("3.000"), SBER: Decimal("300.00")})
    trading: bool = True
    session_type: str = "CORE_TRADING"
    reject_off_step: bool = True
    repeat_after_error: str = "accept"  # accept | reject — тот же ключ после отказа
    repeat_key: str = "reject"  # reject | same | new — тот же ключ после приёма
    band_pct: Decimal | None = None  # цены ниже last*(1-band) отклоняются
    fill_limit: bool = False  # лимитная заявка сразу исполняется (сбой разведки)
    trigger_after_polls: int | None = 1  # стоп срабатывает на N-м GetOrder; None — не срабатывает
    exhaust_clock_after: int | None = None
    exhaust_place: bool = False
    no_money: bool = False  # заявки отклоняются по обеспечению, как на пустом счёте 23.11
    fail_after_place: bool = False  # первый GetOrders после приёма заявки — UNAVAILABLE
    jwt_life_min: int = 15
    streams_die_on_expiry: bool = True
    renew_after_min: int = 13
    streams: list = field(default_factory=list)
    orders: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    transactions: list = field(default_factory=list)
    jwts: dict = field(default_factory=dict)  # jwt -> время выдачи
    auth_calls: int = 0
    calls: list = field(default_factory=list)  # (метод, метаданные)
    clock_calls: int = 0
    _seq: int = 0
    _stop_polls: int = 0

    # --- время --------------------------------------------------------------
    def clock(self) -> datetime:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += timedelta(seconds=secs)
        time.sleep(0.002)  # даём потокам стримов разобрать очередь

    def jwt_expired(self, jwt: str) -> bool:
        issued = self.jwts.get(jwt)
        return issued is None or self.now >= issued + timedelta(minutes=self.jwt_life_min)

    def issue(self) -> str:
        self._seq += 1
        token = f"fake.jwt.{self._seq:04d}.payload-0123456789abcdef"
        self.jwts[token] = self.now
        return token

    def next_id(self, prefix="ord") -> str:
        self._seq += 1
        return f"{prefix}{self._seq:06d}"

    def check(self, method: str, metadata) -> None:
        self.calls.append((method, dict(metadata or ())))
        jwt = dict(metadata or ()).get("authorization")
        if self.jwt_expired(jwt):
            raise FakeRpcError(StatusCode.UNAUTHENTICATED, "JWT истёк или неверен")

    def push(self, state=None, trade=None) -> None:
        for stream in self.streams:
            if stream.kind == "orders" and state is not None:
                stream.queue.put(orders_pb2.SubscribeOrdersResponse(orders=[state]))
            if stream.kind == "trades" and trade is not None:
                stream.queue.put(orders_pb2.SubscribeTradesResponse(trades=[trade]))
            if stream.kind == "order_trade":
                stream.queue.put(orders_pb2.OrderTradeResponse(orders=[state] if state is not None else [],
                                                               trades=[trade] if trade is not None else []))

    # --- исполнение ----------------------------------------------------------
    def execute(self, order, *, price: Decimal | None = None):
        price = price if price is not None else self.last[order.symbol]
        qty = Decimal(order.quantity.value)
        signed = qty if order.side == side_pb2.SIDE_BUY else -qty
        self.positions[order.symbol] = self.positions.get(order.symbol, Decimal(0)) + signed
        state = orders_pb2.OrderState(order_id=self.next_id(), status=S.ORDER_STATUS_FILLED, order=order,
                                      transact_at=stamp(self.now), initial_quantity=order.quantity,
                                      executed_quantity=order.quantity, remaining_quantity=d(0))
        self.orders[state.order_id] = state
        trade = trade_pb2.AccountTrade(trade_id=self.next_id("trd"), symbol=order.symbol, price=d(price),
                                       size=order.quantity, side=order.side, timestamp=stamp(self.now),
                                       order_id=state.order_id, account_id=order.account_id, currency="RUB",
                                       commission=trade_pb2.AccountTrade.Commission(value=d("1.50"), currency="RUB"))
        self.trades.append(trade)
        self.transactions.append(accounts_pb2.Transaction(
            id=self.next_id("tx"), symbol=order.symbol, timestamp=stamp(self.now),
            transaction_category=accounts_pb2.Transaction.TransactionCategory.COMMISSION,
            transaction_name="Комиссия брокера", change=money_pb2.Money(currency_code="RUB", units=-1, nanos=-500000000)))
        self.push(state, trade)
        return state


class _Service:
    def __init__(self, broker: FakeFinam, name: str):
        self.b = broker
        self.name = name

    def __getattr__(self, rpc: str):
        handler = getattr(self.b_api, f"{self.name}__{rpc}")

        def call(request, metadata=None, timeout=None):
            return handler(request, metadata)

        return call

    @property
    def b_api(self):
        return _Api(self.b)


class _Api:
    def __init__(self, broker: FakeFinam):
        self.b = broker

    # auth
    def auth__Auth(self, request, metadata):
        self.b.calls.append(("auth.Auth", dict(metadata or ())))
        if request.secret != SECRET:
            raise FakeRpcError(StatusCode.UNAUTHENTICATED, "неверный секрет")
        self.b.auth_calls += 1
        return auth_pb2.AuthResponse(token=self.b.issue())

    def auth__TokenDetails(self, request, metadata):
        self.b.calls.append(("auth.TokenDetails", dict(metadata or ())))
        if dict(metadata or ()).get("authorization"):  # как у Финама 23.11: JWT только в теле
            raise FakeRpcError(StatusCode.INVALID_ARGUMENT, "Token is invalid or malformed")
        if self.b.jwt_expired(request.token):
            raise FakeRpcError(StatusCode.UNAUTHENTICATED, "JWT истёк или неверен")
        issued = self.b.jwts.get(request.token, self.b.now)
        return auth_pb2.TokenDetailsResponse(
            created_at=stamp(issued), expires_at=stamp(issued + timedelta(minutes=self.b.jwt_life_min)),
            account_ids=self.b.account_ids, readonly=self.b.readonly,
            md_permissions=[auth_pb2.MDPermission(quote_level=auth_pb2.MDPermission.QUOTE_LEVEL_DEPTH_OF_MARKET,
                                                  mic="RTSX")])

    def auth__SubscribeJwtRenewal(self, request, metadata):
        assert not dict(metadata or ()).get("authorization"), "AuthService — без заголовка authorization"
        return RenewalStream(self.b)

    # accounts
    def accounts__GetAccount(self, request, metadata):
        self.b.check("accounts.GetAccount", metadata)
        positions = [accounts_pb2.Position(symbol=symbol, quantity=d(qty), current_price=d(self.b.last[symbol]),
                                           maintenance_margin=d("16000"))
                     for symbol, qty in self.b.positions.items() if qty]
        return accounts_pb2.GetAccountResponse(
            account_id=request.account_id, type="UNION", status="ACCOUNT_ACTIVE", equity=d("100000"),
            unrealized_profit=d("0"), cash=[money_pb2.Money(currency_code="RUB", units=100000)],
            portfolio_mc=accounts_pb2.MC(available_cash=d("90000"), initial_margin=d("0"),
                                         maintenance_margin=d("0")), positions=positions)

    def accounts__Trades(self, request, metadata):
        self.b.check("accounts.Trades", metadata)
        return accounts_pb2.TradesResponse(trades=self.b.trades)

    def accounts__Transactions(self, request, metadata):
        self.b.check("accounts.Transactions", metadata)
        return accounts_pb2.TransactionsResponse(transactions=self.b.transactions)

    # assets
    def assets__Assets(self, request, metadata):
        self.b.check("assets.Assets", metadata)
        return assets_pb2.AssetsResponse(assets=[
            assets_pb2.Asset(symbol=NG, ticker="NGZ6", mic="RTSX", type="FUTURES", name="NG-12.26"),
            assets_pb2.Asset(symbol="NGZ6@OLD", ticker="NGZ6", mic="OLD", type="FUTURES", is_archived=True),
            assets_pb2.Asset(symbol=SBER, ticker="SBER", mic="MISX", type="EQUITIES", name="Сбербанк"),
        ])

    def assets__GetAsset(self, request, metadata):
        self.b.check("assets.GetAsset", metadata)
        if request.symbol == NG:
            return assets_pb2.GetAssetResponse(ticker="NGZ6", mic="RTSX", type="FUTURES", decimals=3, min_step=1,
                                               lot_size=d(100), future_details=assets_pb2.GetAssetResponse.FutureDetails(
                                                   contract_size=d(100)))
        return assets_pb2.GetAssetResponse(ticker="SBER", mic="MISX", type="EQUITIES", decimals=2, min_step=1,
                                           lot_size=d(10))

    def assets__GetAssetParams(self, request, metadata):
        self.b.check("assets.GetAssetParams", metadata)
        from google.protobuf.wrappers_pb2 import BoolValue

        return assets_pb2.GetAssetParamsResponse(
            symbol=request.symbol, is_tradable=BoolValue(value=True), trade_lot_size=1,
            long_initial_margin=money_pb2.Money(currency_code="RUB", units=16000),
            short_initial_margin=money_pb2.Money(currency_code="RUB", units=16500))

    def assets__Schedule(self, request, metadata):
        self.b.check("assets.Schedule", metadata)
        kind = self.b.session_type if self.b.trading else "CLOSED"
        session = assets_pb2.ScheduleResponse.Sessions(type=kind, interval=interval_pb2.Interval(
            start_time=stamp(self.b.now - timedelta(hours=1)), end_time=stamp(self.b.now + timedelta(hours=2))))
        return assets_pb2.ScheduleResponse(symbol=request.symbol, sessions=[session])

    def assets__Clock(self, request, metadata):
        self.b.check("assets.Clock", metadata)
        self.b.clock_calls += 1
        if self.b.exhaust_clock_after is not None and self.b.clock_calls > self.b.exhaust_clock_after:
            raise FakeRpcError(StatusCode.RESOURCE_EXHAUSTED, "Слишком много запросов",
                               trailing=(("grpc-retry-pushback-ms", "5000"), ("x-ratelimit", "200")))
        return assets_pb2.ClockResponse(timestamp=stamp(self.b.now))

    # market data
    def marketdata__LastQuote(self, request, metadata):
        self.b.check("marketdata.LastQuote", metadata)
        last = self.b.last[request.symbol]
        return md_pb2.QuoteResponse(symbol=request.symbol, quote=md_pb2.Quote(
            symbol=request.symbol, last=d(last), bid=d(last), ask=d(last), timestamp=stamp(self.b.now)))

    def marketdata__OrderBook(self, request, metadata):
        self.b.check("marketdata.OrderBook", metadata)
        last = self.b.last[request.symbol]
        return md_pb2.OrderBookResponse(symbol=request.symbol, orderbook=md_pb2.OrderBook(rows=[
            md_pb2.OrderBook.Row(price=d(last), sell_size=d(5)), md_pb2.OrderBook.Row(price=d(last), buy_size=d(3))]))

    # metrics
    def metrics__GetUsageMetrics(self, request, metadata):
        self.b.check("metrics.GetUsageMetrics", metadata)
        return metrics_pb2.GetUsageMetricsResponse(quotas=[metrics_pb2.GetUsageMetricsResponse.QuotaUsage(
            name="assets.Clock", limit=200, remaining=200 - self.b.clock_calls, reset_time=stamp(self.b.now))])

    # orders
    def orders__PlaceOrder(self, order, metadata):
        b = self.b
        b.check("orders.PlaceOrder", metadata)
        if b.exhaust_place:
            raise FakeRpcError(StatusCode.RESOURCE_EXHAUSTED, "Слишком много запросов")
        key = order.client_order_id
        same = [state for state in b.orders.values() if state.order.client_order_id == key]
        if same and any(state.status != S.ORDER_STATUS_REJECTED for state in same):
            if b.repeat_key == "reject":
                raise FakeRpcError(StatusCode.ALREADY_EXISTS, f"заявка с ключом уже есть, счёт {order.account_id}")
            if b.repeat_key == "same":
                return same[0]
        if same and b.repeat_after_error == "reject":
            raise FakeRpcError(StatusCode.INVALID_ARGUMENT, "ключ уже использован")
        step = Decimal("0.001") if order.symbol == NG else Decimal("0.01")
        price_text = order.limit_price.value or order.stop_price.value
        price = Decimal(price_text) if price_text else None
        if price is not None and b.reject_off_step and price % step:
            b.orders[b.next_id("rej")] = orders_pb2.OrderState(order_id="", status=S.ORDER_STATUS_REJECTED,
                                                                order=order)
            raise FakeRpcError(StatusCode.INVALID_ARGUMENT, "цена не кратна шагу")
        if b.no_money:
            raise FakeRpcError(StatusCode.INVALID_ARGUMENT, "INVALID_ARGUMENT: [145]No enough coverage. You need "
                               f"9897.72 RUR, счёт {order.account_id}\x0cНедостаток обеспечения в сумме 9897.72 RUR")
        if (order.type == orders_pb2.ORDER_TYPE_LIMIT and b.band_pct is not None
                and price < b.last[order.symbol] * (1 - b.band_pct / 100)):
            raise FakeRpcError(StatusCode.INVALID_ARGUMENT, "цена вне лимитов")
        if order.type == orders_pb2.ORDER_TYPE_MARKET or (order.type == orders_pb2.ORDER_TYPE_LIMIT and b.fill_limit):
            return b.execute(order)
        status = S.ORDER_STATUS_WATCHING if order.type == orders_pb2.ORDER_TYPE_STOP else S.ORDER_STATUS_NEW
        state = orders_pb2.OrderState(order_id=b.next_id(), status=status, order=order, transact_at=stamp(b.now))
        if order.type == orders_pb2.ORDER_TYPE_LIMIT:
            state.initial_quantity.CopyFrom(order.quantity)
            state.executed_quantity.CopyFrom(d(0))
        b.orders[state.order_id] = state
        b.push(state)
        return state

    def orders__GetOrders(self, request, metadata):
        self.b.check("orders.GetOrders", metadata)
        if self.b.fail_after_place and any(state.order_id for state in self.b.orders.values()):
            self.b.fail_after_place = False
            raise FakeRpcError(StatusCode.UNAVAILABLE, "сеть")
        return orders_pb2.OrdersResponse(orders=[state for state in self.b.orders.values() if state.order_id])

    def orders__GetOrder(self, request, metadata):
        b = self.b
        b.check("orders.GetOrder", metadata)
        state = b.orders.get(request.order_id)
        if state is None:
            raise FakeRpcError(StatusCode.NOT_FOUND, "заявка не найдена")
        if state.order.type == orders_pb2.ORDER_TYPE_STOP and state.status == S.ORDER_STATUS_WATCHING \
                and b.trigger_after_polls is not None:
            b._stop_polls += 1
            if b._stop_polls >= b.trigger_after_polls:
                market = orders_pb2.Order(account_id=state.order.account_id, symbol=state.order.symbol,
                                          quantity=state.order.quantity, side=state.order.side,
                                          type=orders_pb2.ORDER_TYPE_MARKET)
                child = b.execute(market, price=Decimal(state.order.stop_price.value))
                state.status = S.ORDER_STATUS_EXECUTED
                state.triggered_order_id = child.order_id
                b.push(state)
        return state

    def orders__CancelOrder(self, request, metadata):
        self.b.check("orders.CancelOrder", metadata)
        state = self.b.orders.get(request.order_id)
        if state is None:
            raise FakeRpcError(StatusCode.NOT_FOUND, "заявка не найдена")
        if state.status in (S.ORDER_STATUS_FILLED, S.ORDER_STATUS_EXECUTED):
            raise FakeRpcError(StatusCode.FAILED_PRECONDITION, "заявка уже исполнена")
        state.status = S.ORDER_STATUS_CANCELED
        state.withdraw_at.CopyFrom(stamp(self.b.now))
        self.b.push(state)
        return state

    def _stream(self, kind, metadata):
        self.b.check(f"orders.{kind}", metadata)
        stream = FakeStream(self.b, kind, dict(metadata or ()).get("authorization"))
        self.b.streams.append(stream)
        return stream

    def orders__SubscribeOrders(self, request, metadata):
        return self._stream("orders", metadata)

    def orders__SubscribeTrades(self, request, metadata):
        return self._stream("trades", metadata)

    def orders__SubscribeOrderTrade(self, requests, metadata):
        first = next(iter(requests))
        assert first.action == orders_pb2.OrderTradeRequest.ACTION_SUBSCRIBE
        return self._stream("order_trade", metadata)


def stubs(broker: FakeFinam):
    return lambda channel: {name: _Service(broker, name) for name in
                            ("auth", "accounts", "orders", "assets", "marketdata", "metrics")}


def gateway_factory(broker: FakeFinam, **extra):
    def make(secret, **kwargs):
        kwargs.update(extra)
        return FinamGateway(secret, channel_factory=lambda target: None, stub_factory=stubs(broker),
                            sleep=lambda secs: None, clock=broker.clock, **kwargs)

    return make


def reference_factory(tinvest: FakeBroker | None = None):
    tinvest = tinvest or FakeBroker()

    def make(token):
        from src.finam.instruments import ReferenceGateway

        return ReferenceGateway(token, client_factory=lambda value: FakeClient(tinvest, value))

    return make


def recorder(tmp_path) -> CallRecorder:
    return CallRecorder(tmp_path / "responses.jsonl")


__all__ = ["BrokerGateway"]
