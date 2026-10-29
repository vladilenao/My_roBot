"""Узкий адаптер API Т-Инвест для контура на брокере (add-tinvest-orders).

``TInvestBroker`` не знает SDK: он получает отчёты ``OrderReport`` и ошибки
``OrderFailure`` с понятным видом. Котировка без валюты (``Quotation``) — цена
в пунктах; сумма с валютой (``MoneyValue``) у фьючерса — рубли за контракт,
их переводит ``prices.points_from_rub``. Разведка С1: цены сделок биржи в
стриме приходят в пунктах, суммы заявки — в рублях.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

from t_tech.invest import schemas as s
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal

from src.sandbox.gateway import BrokerCallError

# Обрыв или тайм-аут: брокер мог принять заявку — её ищут по ключу (design.md, решение 8).
TRANSPORT_CODES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "UNKNOWN", "CANCELLED", "ABORTED"})
DUPLICATE_DETAILS = "30057"
EPOCH = datetime(1970, 1, 2, tzinfo=timezone.utc)

_STATUS = {
    s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW: "new",
    s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL: "partial",
    s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL: "fill",
    s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: "cancelled",
    s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: "rejected",
}
_DIRECTION = {"BUY": s.OrderDirection.ORDER_DIRECTION_BUY, "SELL": s.OrderDirection.ORDER_DIRECTION_SELL}


@dataclass(frozen=True)
class BrokerTrade:
    """Сделка биржи по заявке: номер, цена, количество (как прислал брокер), время.

    ``in_rub`` — цена пришла суммой (``OrderStage`` в ``GetOrderState``): у фьючерса это рубли за
    контракт. Цена сделки стрима (``OrderTrade``) — котировка в пунктах.
    """

    trade_id: str
    price: Decimal
    quantity: int
    at: datetime
    in_rub: bool = False


@dataclass(frozen=True)
class OrderReport:
    """Состояние заявки у брокера: из ответа ``PostOrder``, ``GetOrderState`` или стрима."""

    broker_order_id: str
    key: str | None
    status: str                        # new · partial · fill · cancelled · rejected · unknown
    lots_requested: int
    lots_executed: int
    instrument_uid: str = ""
    trades: tuple[BrokerTrade, ...] = ()
    executed_rub: Decimal | None = None   # средняя цена исполнения из суммы заявки (рубли у фьючерса)
    temporary: bool = False               # первое событие стрима с временным номером заявки
    at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"fill", "cancelled", "rejected"}


class OrderFailure(Exception):
    """Отказ вызова заявки: ``unknown`` — судьба неизвестна, ``duplicate`` — ключ уже был,
    ``rate_limited`` — повторы шлюза исчерпаны, ``not_found`` — по ключу пусто, ``rejected`` — отказ брокера."""

    def __init__(self, kind: str, code: str = "", details: str = "", message: str = ""):
        super().__init__(kind, code, details, message)
        self.kind, self.code, self.details, self.message = kind, code, details, message

    def __str__(self) -> str:
        return " ".join(part for part in (self.code, self.details, self.message) if part) or self.kind

    @property
    def reason(self) -> str:
        """Причина отказа для журнала: ``broker:<код>``."""
        return f"broker:{self.details or self.code or self.kind}"


def classify(error: BrokerCallError) -> OrderFailure:
    if error.code == "RESOURCE_EXHAUSTED":
        kind = "rate_limited"
    elif error.code == "INVALID_ARGUMENT" and error.details == DUPLICATE_DETAILS:
        kind = "duplicate"
    elif error.code == "NOT_FOUND":
        kind = "not_found"
    elif error.code in TRANSPORT_CODES or not error.code.isupper():
        kind = "unknown"  # gRPC-статус обрыва или исключение транспорта по имени класса
    else:
        kind = "rejected"
    return OrderFailure(kind, error.code, error.details, error.message)


def _present(value) -> bool:
    """Поле oneof задано: у незаданного SDK оставляет служебный объект, а не ``None``."""
    return value is not None and hasattr(value, "order_id")


def _quotation(value) -> Decimal | None:
    if value is None:
        return None
    return quotation_to_decimal(value)


def _when(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _trades(items) -> tuple[BrokerTrade, ...]:
    # Незаданное поле SDK — служебный объект, а не пустой список.
    if not isinstance(items, (list, tuple)):
        return ()
    result = []
    for item in items:
        trade_id = getattr(item, "trade_id", "") or ""
        if not trade_id:
            continue
        in_rub = isinstance(item.price, s.MoneyValue)
        price = money_to_decimal(item.price) if in_rub else _quotation(item.price)
        result.append(BrokerTrade(trade_id, price, int(item.quantity),
                                  _when(getattr(item, "date_time", None) or getattr(item, "execution_time", None))
                                  or datetime.now(timezone.utc), in_rub))
    return tuple(result)


def _rub(value) -> Decimal | None:
    if value is None:
        return None
    amount = money_to_decimal(value)
    return amount if amount > 0 else None


def report_from_state(state) -> OrderReport:
    """``OrderState`` (``GetOrderState``, ``GetOrders``) или ответ ``PostOrder``."""
    return OrderReport(
        broker_order_id=state.order_id, key=getattr(state, "order_request_id", None) or None,
        status=_STATUS.get(state.execution_report_status, "unknown"),
        lots_requested=int(state.lots_requested), lots_executed=int(state.lots_executed),
        instrument_uid=getattr(state, "instrument_uid", "") or "",
        trades=_trades(getattr(state, "stages", ())), executed_rub=_rub(getattr(state, "executed_order_price", None)),
        at=_when(getattr(state, "order_date", None)))


def report_from_stream(state) -> OrderReport:
    """``OrderStateStreamOrderState``: первое событие несёт временный номер (``created_at`` — эпоха)."""
    created = _when(getattr(state, "created_at", None))
    return OrderReport(
        broker_order_id=state.order_id, key=state.order_request_id or None,
        status=_STATUS.get(state.execution_report_status, "unknown"),
        lots_requested=int(state.lots_requested), lots_executed=int(state.lots_executed),
        instrument_uid=getattr(state, "instrument_uid", "") or "", trades=_trades(state.trades),
        executed_rub=_rub(getattr(state, "executed_order_price", None)),
        temporary=created is None or created < EPOCH, at=created)


class TInvestApi:
    """Заявки, поиск по ключу, цены и стрим по счёту робота через шлюз (песочница или боевой)."""

    def __init__(self, gateway, account_id: str):
        self._gateway = gateway
        self._account = account_id

    def _call(self, method: str, **kwargs):
        try:
            return self._gateway.call(method, **kwargs)
        except BrokerCallError as exc:
            raise classify(exc) from None

    def post_limit(self, uid: str, side: str, lots: int, price: Decimal, key: str) -> OrderReport:
        from t_tech.invest.utils import decimal_to_quotation

        response = self._call("orders.post_order", instrument_id=uid, quantity=lots, price=decimal_to_quotation(price),
                              direction=_DIRECTION[side], account_id=self._account,
                              order_type=s.OrderType.ORDER_TYPE_LIMIT, order_id=key)
        return report_from_state(response)

    def post_market(self, uid: str, side: str, lots: int, key: str) -> OrderReport:
        response = self._call("orders.post_order", instrument_id=uid, quantity=lots, direction=_DIRECTION[side],
                              account_id=self._account, order_type=s.OrderType.ORDER_TYPE_MARKET, order_id=key)
        return report_from_state(response)

    def cancel(self, broker_order_id: str) -> None:
        self._call("orders.cancel_order", account_id=self._account, order_id=broker_order_id)

    def order_by_key(self, key: str) -> OrderReport | None:
        """``GetOrderState`` по нашему ключу; ``None`` — у брокера такой заявки нет."""
        try:
            state = self._call("orders.get_order_state", account_id=self._account, order_id=key,
                               order_id_type=s.OrderIdType.ORDER_ID_TYPE_REQUEST)
        except OrderFailure as failure:
            if failure.kind == "not_found":
                return None
            raise
        report = report_from_state(state)
        return report if report.key else replace(report, key=key)

    def last_price(self, uid: str) -> Decimal | None:
        response = self._call("market_data.get_last_prices", instrument_id=[uid])
        for item in response.last_prices:
            if item.instrument_uid == uid or len(response.last_prices) == 1:
                price = _quotation(item.price)
                return price if price and price > 0 else None
        return None

    def price_band(self, uid: str) -> tuple[Decimal, Decimal] | None:
        """Коридор цены инструмента (``limit_down``, ``limit_up``) в пунктах; нет данных — ``None``."""
        book = self._call("market_data.get_order_book", instrument_id=uid, depth=1)
        low, high = _quotation(book.limit_down), _quotation(book.limit_up)
        if not low or not high or low <= 0 or high <= low:
            return None
        return low, high

    def instrument_uid(self, ticker: str, future: bool) -> str:
        """UID инструмента по тикеру: фьючерс — секция SPBFUT, акция — TQBR."""
        response = self._call("instruments.get_instrument_by", id_type=s.InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                              class_code="SPBFUT" if future else "TQBR", id=ticker)
        return response.instrument.uid

    def futures_margin(self, uid: str) -> dict:
        response = self._call("instruments.get_futures_margin", instrument_id=uid)
        return {"go_buy": money_to_decimal(response.initial_margin_on_buy),
                "go_sell": money_to_decimal(response.initial_margin_on_sell),
                "price_step": _quotation(response.min_price_increment),
                "step_rub": _quotation(response.min_price_increment_amount)}

    def order_stream(self) -> Iterator[OrderReport | None]:
        """Отчёты ``OrderStateStream`` по счёту робота; подписка и пинг — ``None``: соединение живо."""
        request = s.OrderStateStreamRequest(accounts=[self._account])
        for message in self._gateway.stream("orders_stream.order_state_stream", request=request):
            state = getattr(message, "order_state", None)
            yield report_from_stream(state) if _present(state) else None
