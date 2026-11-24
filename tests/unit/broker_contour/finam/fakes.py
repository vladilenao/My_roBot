"""Фейковый Finam Trade API для адаптера контура «Счёт Финам» (add-finam-contour): заглушки gRPC без сети.

Свой фейк, не разведки Ф0: тесты контура не зависят от тестов разведки. Секрет, номер счёта и JWT —
заведомо фейковые и длинные, чтобы проверки утечек искали их во всех файлах.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from src.broker_contour.port import InstrumentCard
from src.finam.proto.google.type import decimal_pb2, money_pb2
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2 as auth_pb2
from src.finam.proto.grpc.tradeapi.v1.metrics import usage_metrics_service_pb2 as metrics_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2

SECRET = "fake-finam-contour-secret-0123456789abcdef"
ACCOUNT_ID = "FAKE-FINAM-CONTOUR-ACC-55443322"
OTHER_ACCOUNT_ID = "FAKE-FINAM-CONTOUR-ACC-99887766"
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)          # 12:00 МСК
WINDOW = datetime(2026, 11, 25, 2, 30, tzinfo=timezone.utc)      # 05:30 МСК — окно обслуживания
SI, NG, SBER = "SIZ6@RTSX", "NGZ6@RTSX", "SBER@MISX"
SI_CARD = InstrumentCard("uid-si-12-26", "SiZ6", "future", "Si-12.26", 1, Decimal("1"))
NG_CARD = InstrumentCard("uid-ng-12-26", "NGZ6", "future", "NG-12.26", 1, Decimal("0.001"))
SBER_CARD = InstrumentCard("uid-sber", "SBER", "share", "SBER", 10, Decimal("0.01"))
S = orders_pb2.OrderStatus
T = orders_pb2.OrderType


def d(value) -> decimal_pb2.Decimal:
    return decimal_pb2.Decimal(value=str(value))


def money(value, currency="RUB") -> money_pb2.Money:
    amount = Decimal(str(value))
    units = int(amount)
    return money_pb2.Money(currency_code=currency, units=units, nanos=int((amount - units) * 10**9))


def stamp(moment: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment)
    return result


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = ""):
        super().__init__()
        self._code, self._details = code, details

    def code(self):
        return self._code

    def details(self):
        return self._details


class FakeCatalog:
    """Каталог карточек ядра: тикер → карточка."""

    def __init__(self, *cards: InstrumentCard):
        self.cards = {(card.ticker.upper(), card.kind): card for card in cards}
        self.calls: list = []

    def by_uid(self, uid):
        return next((card for card in self.cards.values() if card.uid == uid), None)

    def by_ticker(self, ticker, kind):
        self.calls.append((ticker, kind))
        return self.cards.get((ticker.upper(), kind))


@dataclass
class FakeFinam:
    """Состояние счёта Финама и журнал вызовов: ``(метод, заголовок authorization или None)``."""

    account_ids: list = field(default_factory=lambda: [ACCOUNT_ID])
    readonly: bool = True
    cash: list = field(default_factory=list)             # [(сумма, валюта)]
    equity: Decimal | None = None
    positions: list = field(default_factory=list)        # [(symbol, quantity)]
    orders: list = field(default_factory=list)           # [(symbol, тип, статус)]
    margins: list = field(default_factory=lambda: [("9889.08", "9889.08")])
    quotas: dict = field(default_factory=lambda: {"GetAccount": 200, "GetOrders": 200, "PlaceOrder": 200,
                                                  "CancelOrder": 200, "Assets": 200})
    reset_in: dict = field(default_factory=dict)          # метод → секунд до сброса квоты
    failures: dict = field(default_factory=dict)          # метод → список исключений по очереди
    calls: list = field(default_factory=list)
    issued: list = field(default_factory=list)            # выданные JWT по порядку
    renewal: "queue.Queue" = field(default_factory=queue.Queue)   # JWT, которые пришлёт стрим обновления
    renewal_open: bool = True                             # False — стрим обновления не открывается
    renewal_opened: int = 0
    clock: object = None

    def jwt(self) -> str:
        token = f"fake-finam-jwt-{len(self.issued) + 1:03d}-{'x' * 24}"
        self.issued.append(token)
        return token

    def _fail(self, method: str):
        pending = self.failures.get(method)
        if pending:
            raise pending.pop(0)

    def record(self, method: str, metadata) -> None:
        header = dict(metadata or ()).get("authorization")
        self.calls.append((method, header))
        self._fail(method)

    def names(self) -> list[str]:
        return [method for method, _ in self.calls]

    # --- ответы ---------------------------------------------------------
    def assets(self):
        return assets_pb2.AssetsResponse(assets=[
            assets_pb2.Asset(symbol="SIZ6@FORTS", ticker="SiZ6", mic="FORTS"),
            assets_pb2.Asset(symbol=SI, ticker="SiZ6", mic="RTSX"),
            assets_pb2.Asset(symbol=NG, ticker="NGZ6", mic="RTSX"),
            assets_pb2.Asset(symbol="SBER@MISX-OLD", ticker="SBER", mic="MISX", is_archived=True),
            assets_pb2.Asset(symbol=SBER, ticker="SBER", mic="MISX"),
        ])

    ASSET_INFO = {SI: (1, 0, "1000"), NG: (1, 3, "100"), SBER: (1, 2, "10")}

    def asset(self, symbol):
        step, decimals, lot = self.ASSET_INFO[symbol]
        return assets_pb2.GetAssetResponse(ticker=symbol.split("@")[0], mic=symbol.split("@")[1], min_step=step,
                                           decimals=decimals, lot_size=d(lot))

    def params(self, symbol):
        buy, sell = self.margins.pop(0) if len(self.margins) > 1 else self.margins[0]
        return assets_pb2.GetAssetParamsResponse(symbol=symbol, trade_lot_size=1, long_initial_margin=money(buy),
                                                 short_initial_margin=money(sell))

    def account(self):
        response = accounts_pb2.GetAccountResponse(account_id=ACCOUNT_ID, type="UNION", status="ACCOUNT_ACTIVE")
        if self.equity is not None:
            response.equity.CopyFrom(d(self.equity))
        response.cash.extend(money(amount, currency) for amount, currency in self.cash)
        response.positions.extend(accounts_pb2.Position(symbol=symbol, quantity=d(quantity))
                                  for symbol, quantity in self.positions)
        return response

    def order_states(self):
        return orders_pb2.OrdersResponse(orders=[
            orders_pb2.OrderState(order_id=f"{index:019d}", status=status,
                                  order=orders_pb2.Order(symbol=symbol, type=kind))
            for index, (symbol, kind, status) in enumerate(self.orders, start=1)])

    def usage(self):
        now = self.clock() if self.clock else NOW
        return metrics_pb2.GetUsageMetricsResponse(quotas=[
            metrics_pb2.GetUsageMetricsResponse.QuotaUsage(
                name=f"grpc.tradeapi.v1.Service/{name}", limit=limit, remaining=limit,
                reset_time=stamp(now + timedelta(seconds=self.reset_in.get(name, 60))))
            for name, limit in self.quotas.items()])


class _Stream:
    """Стрим ``SubscribeJwtRenewal``: первый JWT сразу, дальше — из очереди; ``cancel`` — конец."""

    def __init__(self, fake: FakeFinam):
        self._fake, self._cancelled = fake, threading.Event()
        self._first = True

    def cancel(self):
        self._cancelled.set()

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            return auth_pb2.SubscribeJwtRenewalResponse(token=self._fake.jwt())
        while not self._cancelled.is_set():
            try:
                item = self._fake.renewal.get(timeout=0.01)
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            self._fake.issued.append(item)
            return auth_pb2.SubscribeJwtRenewalResponse(token=item)
        raise FakeRpcError(grpc.StatusCode.CANCELLED, "cancelled")


class _Service:
    def __init__(self, fake: FakeFinam, handlers: dict):
        self._fake, self._handlers = fake, handlers

    def __getattr__(self, name):
        handler = self._handlers[name]

        def method(request, metadata=None, timeout=None):
            self._fake.record(name, metadata)
            return handler(request)
        return method


def stubs(fake: FakeFinam):
    """``stub_factory`` транспорта: сервисы фейка вместо заглушек gRPC."""

    def renewal(request):
        if request.secret != SECRET:
            raise FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, "bad secret")
        if not fake.renewal_open:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "renewal closed")
        fake.renewal_opened += 1
        return _Stream(fake)

    def auth(request):
        if request.secret != SECRET:
            raise FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, "bad secret")
        return auth_pb2.AuthResponse(token=fake.jwt())

    def token_details(request):
        return auth_pb2.TokenDetailsResponse(account_ids=list(fake.account_ids), readonly=fake.readonly)

    def forbidden(request):
        raise AssertionError("контур на чтение не ставит заявок")

    return lambda channel: {
        "auth": _Service(fake, {"Auth": auth, "TokenDetails": token_details, "SubscribeJwtRenewal": renewal}),
        "accounts": _Service(fake, {"GetAccount": lambda request: fake.account()}),
        "orders": _Service(fake, {"GetOrders": lambda request: fake.order_states(), "PlaceOrder": forbidden,
                                  "CancelOrder": forbidden, "PlaceSLTPOrder": forbidden}),
        "assets": _Service(fake, {"Assets": lambda request: fake.assets(),
                                  "GetAsset": lambda request: fake.asset(request.symbol),
                                  "GetAssetParams": lambda request: fake.params(request.symbol)}),
        "metrics": _Service(fake, {"GetUsageMetrics": lambda request: fake.usage()}),
    }


def transport(fake: FakeFinam, *, clock=None, sleep=None, monotonic=None, backoff=(0.01,)):
    """Транспорт адаптера на фейке; ``sleep`` по умолчанию копит паузы в ``fake.slept``."""
    from src.broker_contour.finam.transport import FinamTransport

    fake.slept = []
    fake.clock = clock or (lambda: NOW)
    return FinamTransport(SECRET, account_id=ACCOUNT_ID, channel_factory=lambda target: None,
                          stub_factory=stubs(fake), clock=fake.clock,
                          sleep=sleep or fake.slept.append, monotonic=monotonic or (lambda: 0.0),
                          renewal_backoff=backoff)
