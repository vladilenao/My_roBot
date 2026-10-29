"""Ручные действия в песочнице: состояние счёта, пополнение, заявки, уборка."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

from t_tech.invest.schemas import (
    OrderDirection,
    OrderExecutionReportStatus,
    OrderType,
    StopOrderDirection,
    StopOrderStatusOption,
    StopOrderType,
)
from t_tech.invest.utils import decimal_to_money, money_to_decimal, quotation_to_decimal

from src.sandbox.account import RobotAccount
from src.sandbox.gateway import BrokerCallError, SandboxGateway
from src.sandbox.instruments import SandboxInstrument, short_name_by_uid

Out = Callable[[str], None]

STATUS_NAMES = {
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL: "исполнена",
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL: "исполнена частично",
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW: "новая",
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: "снята",
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: "отклонена",
}
ACTIVE_STATUSES = {
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
    OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
}
STOP_TYPE_NAMES = {
    StopOrderType.STOP_ORDER_TYPE_STOP_LOSS: "стоп-лосс",
    StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT: "тейк-профит",
    StopOrderType.STOP_ORDER_TYPE_STOP_LIMIT: "стоп-лимит",
}
RESET_WARNING = ("Внимание: работающий контур «Песочница» увидит снятие заявок и закрытие позиций "
                 "как чужие действия на счёте.")


def lots_word(lots: int) -> str:
    tail = abs(lots) % 100
    if 11 <= tail <= 14:
        return f"{lots} лотов"
    return f"{lots} " + {1: "лот", 2: "лота", 3: "лота", 4: "лота"}.get(tail % 10, "лотов")


def rub(value: Decimal) -> str:
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    return text.replace(",", " ") + " ₽"


def price(value: Decimal) -> str:
    return format(value.normalize(), "f")


def side_word(direction) -> str:
    buy = direction in (OrderDirection.ORDER_DIRECTION_BUY, StopOrderDirection.STOP_ORDER_DIRECTION_BUY)
    return "покупка" if buy else "продажа"


def new_key() -> str:
    """Ключ идемпотентности заявки: новый на каждую заявку."""
    return str(uuid.uuid4())


def rub_balance(gateway: SandboxGateway, account: RobotAccount) -> Decimal:
    positions = gateway.call("operations.get_positions", account_id=account.account_id)
    return sum((money_to_decimal(item) for item in positions.money if item.currency.lower() == "rub"), Decimal(0))


def open_positions(gateway: SandboxGateway, account: RobotAccount) -> list:
    """Позиции портфеля без валюты: ``(instrument_uid, лоты, средняя цена)``."""
    portfolio = gateway.call("operations.get_portfolio", account_id=account.account_id)
    result = []
    for item in portfolio.positions:
        if item.instrument_type == "currency":
            continue
        lots = int(quotation_to_decimal(item.quantity_lots))
        if lots:
            result.append((item.instrument_uid, lots, money_to_decimal(item.average_position_price)))
    return result


def active_orders(gateway: SandboxGateway, account: RobotAccount) -> list:
    orders = gateway.call("orders.get_orders", account_id=account.account_id).orders
    return [order for order in orders if order.execution_report_status in ACTIVE_STATUSES]


def active_stop_orders(gateway: SandboxGateway, account: RobotAccount) -> list:
    return list(gateway.call("stop_orders.get_stop_orders", account_id=account.account_id,
                             status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE).stop_orders)


def show_account(gateway: SandboxGateway, account: RobotAccount, out: Out) -> None:
    names: dict[str, str] = {}
    out(f"Счёт песочницы {account.masked}")
    out(f"Рубли: {rub(rub_balance(gateway, account))}")
    positions = open_positions(gateway, account)
    out("Позиции:" + ("" if positions else " нет"))
    for uid, lots, average in positions:
        out(f"  {short_name_by_uid(gateway, uid, names)} {lots_word(lots)}, средняя {price(average)}")
    orders = active_orders(gateway, account)
    out("Заявки:" + ("" if orders else " нет"))
    for order in orders:
        left = order.lots_requested - order.lots_executed
        out(f"  {short_name_by_uid(gateway, order.instrument_uid, names)} {side_word(order.direction)} "
            f"{lots_word(left)} по {price(money_to_decimal(order.initial_security_price))}, "
            f"{STATUS_NAMES.get(order.execution_report_status, order.execution_report_status.name)}")
    stops = active_stop_orders(gateway, account)
    out("Стоп-заявки:" + ("" if stops else " нет"))
    for stop in stops:
        out(f"  {short_name_by_uid(gateway, stop.instrument_uid, names)} {side_word(stop.direction)} "
            f"{lots_word(stop.lots_requested)}, стоп {price(money_to_decimal(stop.stop_price))}, "
            f"{STOP_TYPE_NAMES.get(stop.order_type, stop.order_type.name)}")


def pay_in(gateway: SandboxGateway, account: RobotAccount, amount: int, out: Out) -> None:
    response = gateway.call("sandbox.sandbox_pay_in", account_id=account.account_id,
                            amount=decimal_to_money(Decimal(amount), "rub"))
    out(f"Счёт песочницы {account.masked} пополнен на {rub(Decimal(amount))}. "
        f"Баланс: {rub(money_to_decimal(response.balance))}")


def market_order(gateway: SandboxGateway, account: RobotAccount, instrument: SandboxInstrument, lots: int,
                 direction: OrderDirection, *, key: str | None = None):
    return gateway.call("orders.post_order", instrument_id=instrument.uid, quantity=lots, direction=direction,
                        account_id=account.account_id, order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=key or new_key())


def describe_fill(response) -> str:
    status = STATUS_NAMES.get(response.execution_report_status, response.execution_report_status.name)
    text = f"Заявка {status}: исполнено {response.lots_executed} из {response.lots_requested}"
    if response.lots_executed:
        text += f", цена {price(money_to_decimal(response.executed_order_price))}"
    commission = money_to_decimal(response.executed_commission) or money_to_decimal(response.initial_commission)
    if commission:
        text += f", комиссия {rub(commission)}"
    return text


def manual_order(gateway: SandboxGateway, account: RobotAccount, instrument: SandboxInstrument, lots: int,
                 direction: OrderDirection, out: Out) -> None:
    out(f"{side_word(direction).capitalize()} {instrument.short_name} {lots_word(lots)} по рынку")
    out(describe_fill(market_order(gateway, account, instrument, lots, direction)))


def reset(gateway: SandboxGateway, account: RobotAccount, out: Out) -> bool:
    """Снять заявки и стоп-заявки, закрыть позиции. ``False`` — что-то не закрылось."""
    out(RESET_WARNING)
    names: dict[str, str] = {}
    clean = True
    for order in active_orders(gateway, account):
        try:
            gateway.call("orders.cancel_order", account_id=account.account_id, order_id=order.order_id)
        except BrokerCallError as exc:
            clean = False
            out(f"Заявка {short_name_by_uid(gateway, order.instrument_uid, names)} не снята: {exc}")
    for stop in active_stop_orders(gateway, account):
        try:
            gateway.call("stop_orders.cancel_stop_order", account_id=account.account_id,
                         stop_order_id=stop.stop_order_id)
        except BrokerCallError as exc:
            clean = False
            out(f"Стоп-заявка {short_name_by_uid(gateway, stop.instrument_uid, names)} не снята: {exc}")
    for uid, lots, _ in open_positions(gateway, account):
        direction = OrderDirection.ORDER_DIRECTION_SELL if lots > 0 else OrderDirection.ORDER_DIRECTION_BUY
        try:
            gateway.call("orders.post_order", instrument_id=uid, quantity=abs(lots), direction=direction,
                         account_id=account.account_id, order_type=OrderType.ORDER_TYPE_MARKET, order_id=new_key())
        except BrokerCallError as exc:
            clean = False
            out(f"Позиция {short_name_by_uid(gateway, uid, names)} не закрыта: {exc}")
    show_account(gateway, account, out)
    return clean
