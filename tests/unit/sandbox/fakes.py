"""Фейковая песочница Т-Инвест без сети: сервисы SDK поверх простого состояния счёта."""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from grpc import StatusCode
from t_tech.invest import schemas as s
from t_tech.invest.exceptions import RequestError
from t_tech.invest.utils import decimal_to_quotation, money_to_decimal, quotation_to_decimal

ACCOUNT_ID = "sbx-0000-1111-2222-a1b2"
TOKEN = "fake-sandbox-token-0123456789"
NG_UID = "uid-ng-12-26"
SBER_UID = "uid-sber"
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)


def money(value) -> s.MoneyValue:
    q = decimal_to_quotation(Decimal(str(value)))
    return s.MoneyValue(currency="rub", units=q.units, nano=q.nano)


def quote(value) -> s.Quotation:
    return decimal_to_quotation(Decimal(str(value)))


def broker_reject(code="30079", message="Инструмент недоступен для торгов"):
    return RequestError(StatusCode.INVALID_ARGUMENT, code, SimpleNamespace(message=message))


@dataclass
class FakeBroker:
    accounts: list = field(default_factory=list)
    rub: Decimal = Decimal(0)
    prices: list = field(default_factory=lambda: [Decimal("3.000")])
    trading: s.SecurityTradingStatus = s.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING
    reject_market: bool = False
    executes_stops: bool = True
    repeat_key: str = "same"  # same | new | error
    margin_works: bool = True
    fail_operations: bool = False
    exhaust_market: int = 0  # столько рыночных заявок подряд получат RESOURCE_EXHAUSTED
    positions: dict = field(default_factory=dict)  # uid -> lots
    orders: dict = field(default_factory=dict)  # order_id -> OrderState
    stops: dict = field(default_factory=dict)  # stop_id -> StopOrder
    operations: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    stream: "queue.Queue" = field(default_factory=queue.Queue)
    closed: bool = False
    _price_index: int = 0
    _seq: int = 0

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def last_price(self) -> Decimal:
        value = self.prices[min(self._price_index, len(self.prices) - 1)]
        self._price_index += 1
        return Decimal(str(value))

    def push(self, order: s.OrderState) -> None:
        trades = [s.OrderTrade(date_time=stage.execution_time, price=quote(money_to_decimal(stage.price)),
                               quantity=stage.quantity, trade_id=stage.trade_id) for stage in (order.stages if isinstance(order.stages, list) else ())]
        self.stream.put(s.OrderStateStreamResponse(order_state=s.OrderStateStreamOrderState(
            order_id=order.order_id, order_request_id=order.order_request_id,
            execution_report_status=order.execution_report_status, lots_executed=order.lots_executed,
            lots_requested=order.lots_requested, instrument_uid=order.instrument_uid, trades=trades)))

    def fill(self, uid: str, lots: int, direction, key: str) -> s.OrderState:
        sign = 1 if direction == s.OrderDirection.ORDER_DIRECTION_BUY else -1
        self.positions[uid] = self.positions.get(uid, 0) + sign * lots
        if self.positions[uid] == 0:
            del self.positions[uid]
        price = Decimal("3.000")
        order = s.OrderState(
            order_id=self.next_id("ord"), order_request_id=key, instrument_uid=uid, direction=direction,
            execution_report_status=s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
            lots_requested=lots, lots_executed=lots, initial_security_price=money(price),
            executed_order_price=money(price), executed_commission=money("1.5"), initial_commission=money("1.5"),
            stages=[s.OrderStage(price=money(price), quantity=lots, trade_id=self.next_id("trade"),
                                 execution_time=NOW)])
        self.orders[order.order_id] = order
        kind = s.OperationType.OPERATION_TYPE_BUY if sign > 0 else s.OperationType.OPERATION_TYPE_SELL
        executed = s.OperationState.OPERATION_STATE_EXECUTED
        self.operations.append(s.OperationItem(type=kind, state=executed, instrument_uid=uid,
                                               commission=money("-1.5"), payment=money(-price)))
        self.operations.append(s.OperationItem(type=s.OperationType.OPERATION_TYPE_BROKER_FEE, state=executed,
                                               instrument_uid=uid, commission=s.MoneyValue(currency="", units=0, nano=0),
                                               payment=money("-1.5")))
        self.push(order)
        return order


class _Service:
    def __init__(self, broker: FakeBroker, name: str):
        self.broker = broker
        self.name = name

    def __getattr__(self, method: str):
        handler = getattr(self.broker_api, f"{self.name}__{method}")

        def call(*args, **kwargs):
            self.broker.calls.append((f"{self.name}.{method}", kwargs))
            return handler(*args, **kwargs)

        return call

    @property
    def broker_api(self):
        return _Api(self.broker)


class _Api:
    """Ответы сервисов: ``<сервис>__<метод>``."""

    def __init__(self, broker: FakeBroker):
        self.b = broker

    # users / sandbox
    def users__get_accounts(self):
        return s.GetAccountsResponse(accounts=list(self.b.accounts))

    def sandbox__open_sandbox_account(self, name=""):
        self.b.accounts.append(s.Account(id=ACCOUNT_ID, name=name, status=s.AccountStatus.ACCOUNT_STATUS_OPEN))
        return s.OpenSandboxAccountResponse(account_id=ACCOUNT_ID)

    def sandbox__sandbox_pay_in(self, account_id="", amount=None):
        self.b.rub += money_to_decimal(amount)
        return s.SandboxPayInResponse(balance=money(self.b.rub))

    def users__get_user_tariff(self):
        return s.GetUserTariffResponse(
            unary_limits=[s.UnaryLimit(limit_per_minute=100, methods=[
                "tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder"])],
            stream_limits=[s.StreamLimit(limit=8, open=0, streams=[
                "tinkoff.public.invest.api.contract.v1.OrdersStreamService/OrderStateStream"])])

    def users__get_margin_attributes(self, account_id=""):
        if not self.b.margin_works:
            raise RequestError(StatusCode.UNIMPLEMENTED, "70001", SimpleNamespace(message="Метод недоступен"))
        return s.GetMarginAttributesResponse(liquid_portfolio=money(self.b.rub))

    # operations
    def operations__get_positions(self, account_id=""):
        return s.PositionsResponse(
            money=[money(self.b.rub)], securities=[],
            futures=[s.PositionsFutures(instrument_uid=uid, balance=lots) for uid, lots in self.b.positions.items()])

    def operations__get_portfolio(self, account_id=""):
        items = [s.PortfolioPosition(instrument_type="currency", quantity_lots=quote(self.b.rub),
                                     instrument_uid="rub", average_position_price=money(1))]
        items += [s.PortfolioPosition(instrument_type="futures", quantity_lots=quote(lots), instrument_uid=uid,
                                      average_position_price=money("3.000"))
                  for uid, lots in self.b.positions.items()]
        return s.PortfolioResponse(positions=items)

    def operations__get_operations_by_cursor(self, request):
        if self.b.fail_operations:
            raise RequestError(StatusCode.UNAVAILABLE, "", SimpleNamespace(message=""))
        return s.GetOperationsByCursorResponse(items=list(self.b.operations))

    # orders
    def orders__get_orders(self, account_id=""):
        return s.GetOrdersResponse(orders=list(self.b.orders.values()))

    def orders__get_max_lots(self, request):
        return s.GetMaxLotsResponse(buy_limits=s.BuyLimitsView(buy_max_lots=0, buy_max_market_lots=0),
                                    buy_margin_limits=s.BuyLimitsView(buy_max_lots=160, buy_max_market_lots=158))

    def orders__post_order(self, *, instrument_id="", quantity=0, price=None, direction=None, account_id="",
                           order_type=None, order_id=""):
        existing = next((o for o in self.b.orders.values() if o.order_request_id == order_id), None)
        if existing is not None:
            if self.b.repeat_key == "error":
                raise RequestError(StatusCode.ALREADY_EXISTS, "30057", SimpleNamespace(message="Дубль ключа"))
            if self.b.repeat_key == "same":
                return self._response(existing)
        if price is not None and quotation_to_decimal(price) > Decimal("3.300"):
            raise RequestError(StatusCode.INVALID_ARGUMENT, "30010", SimpleNamespace(message="Цена вне коридора"))
        if order_type == s.OrderType.ORDER_TYPE_MARKET:
            if self.b.reject_market:
                raise broker_reject()
            if self.b.exhaust_market > 0:
                self.b.exhaust_market -= 1
                raise RequestError(StatusCode.RESOURCE_EXHAUSTED, "", SimpleNamespace(message="", ratelimit_reset=None))
            return self._response(self.b.fill(instrument_id, quantity, direction, order_id))
        order = s.OrderState(
            order_id=self.b.next_id("ord"), order_request_id=order_id, instrument_uid=instrument_id,
            direction=direction, execution_report_status=s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
            lots_requested=quantity, lots_executed=0, initial_security_price=money(quotation_to_decimal(price)),
            executed_order_price=money(0), executed_commission=money(0), initial_commission=money(0))
        self.b.orders[order.order_id] = order
        return self._response(order)

    @staticmethod
    def _response(order: s.OrderState) -> s.PostOrderResponse:
        return s.PostOrderResponse(
            order_id=order.order_id, order_request_id=order.order_request_id,
            execution_report_status=order.execution_report_status, lots_requested=order.lots_requested,
            lots_executed=order.lots_executed, executed_order_price=order.executed_order_price,
            executed_commission=order.executed_commission, initial_commission=order.initial_commission)

    def orders__get_order_state(self, account_id="", order_id="", order_id_type=None):
        if order_id_type == s.OrderIdType.ORDER_ID_TYPE_REQUEST:
            found = next((o for o in self.b.orders.values() if o.order_request_id == order_id), None)
            if found is None:
                raise RequestError(StatusCode.NOT_FOUND, "50005", SimpleNamespace(message="Order not found"))
            return found
        return self.b.orders[order_id]

    def orders__cancel_order(self, account_id="", order_id=""):
        order = self.b.orders[order_id]
        order.execution_report_status = s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED
        self.b.push(order)
        return s.CancelOrderResponse()

    # stop orders
    def stop_orders__post_stop_order(self, *, instrument_id="", quantity=0, stop_price=None, direction=None,
                                     account_id="", expiration_type=None, stop_order_type=None, order_id=""):
        stop = s.StopOrder(stop_order_id=self.b.next_id("stop"), instrument_uid=instrument_id,
                           lots_requested=quantity, direction=direction, order_type=stop_order_type,
                           stop_price=money(quotation_to_decimal(stop_price)),
                           status=s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE)
        self.b.stops[stop.stop_order_id] = stop
        return s.PostStopOrderResponse(stop_order_id=stop.stop_order_id)

    def stop_orders__get_stop_orders(self, account_id="", status=None, from_=None, to=None):
        items = list(self.b.stops.values())
        if status == s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE:
            items = [i for i in items if i.status == s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE]
        return s.GetStopOrdersResponse(stop_orders=items)

    def stop_orders__cancel_stop_order(self, account_id="", stop_order_id=""):
        self.b.stops[stop_order_id].status = s.StopOrderStatusOption.STOP_ORDER_STATUS_CANCELED
        return s.CancelStopOrderResponse()

    # market data / instruments
    def market_data__get_trading_status(self, instrument_id=""):
        return s.GetTradingStatusResponse(trading_status=self.b.trading)

    def market_data__get_last_prices(self, instrument_id=None):
        price = self.b.last_price()
        if self.b.executes_stops:
            for stop in self.b.stops.values():
                if stop.status == s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE \
                        and price <= money_to_decimal(stop.stop_price):
                    stop.status = s.StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED
                    self.b.fill(stop.instrument_uid, stop.lots_requested, s.OrderDirection.ORDER_DIRECTION_SELL, "")
        return s.GetLastPricesResponse(last_prices=[s.LastPrice(price=quote(price), instrument_uid=instrument_id[0])])

    def market_data__get_order_book(self, instrument_id="", depth=0):
        return s.GetOrderBookResponse(limit_down=quote("2.700"), limit_up=quote("3.300"))

    def instruments__futures(self, instrument_status=None):
        now = datetime.now(timezone.utc)
        return s.FuturesResponse(instruments=[
            s.Future(ticker="NGZ6", name="NG-12.26 Природный газ", expiration_date=now + timedelta(days=31)),
            s.Future(ticker="NGX6", name="NG-11.26 Природный газ", expiration_date=now + timedelta(days=1)),
        ])

    def instruments__get_instrument_by(self, id_type=None, class_code="", id=""):
        cards = {
            "NGZ6": s.Instrument(uid=NG_UID, ticker="NGZ6", lot=1, min_price_increment=quote("0.001"),
                                 name="NG-12.26 Природный газ", instrument_type="futures"),
            "SBER": s.Instrument(uid=SBER_UID, ticker="SBER", lot=10, min_price_increment=quote("0.01"),
                                 name="Сбер Банк", instrument_type="share"),
        }
        by_uid = {card.uid: card for card in cards.values()}
        card = cards.get(id) or by_uid.get(id)
        if card is None:
            raise RequestError(StatusCode.NOT_FOUND, "50002", SimpleNamespace(message="Инструмент не найден"))
        return s.InstrumentResponse(instrument=card)

    def instruments__get_futures_margin(self, instrument_id=""):
        return s.GetFuturesMarginResponse(min_price_increment_amount=quote("7.9"))

    # stream
    def orders_stream__order_state_stream(self, request=None):
        yield s.OrderStateStreamResponse(subscription=s.SubscriptionResponse(
            status=s.ResultSubscriptionStatus.RESULT_SUBSCRIPTION_STATUS_OK))
        while not self.b.closed:
            try:
                yield self.b.stream.get(timeout=0.05)
            except queue.Empty:
                continue


class FakeServices:
    def __init__(self, broker: FakeBroker):
        for name in ("users", "sandbox", "operations", "orders", "stop_orders", "market_data", "instruments",
                     "orders_stream"):
            setattr(self, name, _Service(broker, name))


class FakeClient:
    def __init__(self, broker: FakeBroker, token: str):
        self.broker = broker
        self.token = token

    def __enter__(self):
        return FakeServices(self.broker)

    def __exit__(self, *exc):
        self.broker.closed = True
        return False


def robot_account(broker: FakeBroker) -> None:
    broker.accounts.append(s.Account(id=ACCOUNT_ID, name="My_roBot", status=s.AccountStatus.ACCOUNT_STATUS_OPEN))
