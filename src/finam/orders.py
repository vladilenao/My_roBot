"""Заявки разведки Финама: постановка, поиск, описание, стримы и уборка (add-finam-probe, решения 5–7)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from src.finam.checks import KEY_PREFIX, num, ts
from src.finam.gateway import FinamGateway, StreamHandle, iterate
from src.finam.instruments import Instruments, dec
from src.finam.proto.google.type import decimal_pb2
from src.finam.proto.grpc.tradeapi.v1 import side_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.finam.records import mask_account
from src.sandbox.gateway import BrokerCallError

OrderStatus = orders_pb2.OrderStatus
FINAL_STATUSES = frozenset({
    OrderStatus.ORDER_STATUS_FILLED, OrderStatus.ORDER_STATUS_CANCELED, OrderStatus.ORDER_STATUS_REJECTED,
    OrderStatus.ORDER_STATUS_EXPIRED, OrderStatus.ORDER_STATUS_FAILED, OrderStatus.ORDER_STATUS_DENIED_BY_BROKER,
    OrderStatus.ORDER_STATUS_REJECTED_BY_EXCHANGE, OrderStatus.ORDER_STATUS_EXECUTED,
    OrderStatus.ORDER_STATUS_DISABLED, OrderStatus.ORDER_STATUS_REPLACED, OrderStatus.ORDER_STATUS_DONE_FOR_DAY,
    OrderStatus.ORDER_STATUS_SL_EXECUTED, OrderStatus.ORDER_STATUS_TP_EXECUTED,
})
FILLED_STATUSES = frozenset({OrderStatus.ORDER_STATUS_FILLED, OrderStatus.ORDER_STATUS_PARTIALLY_FILLED})
SIDE_NAMES = {side_pb2.SIDE_BUY: "покупка", side_pb2.SIDE_SELL: "продажа"}


def decimal(value: Decimal) -> decimal_pb2.Decimal:
    return decimal_pb2.Decimal(value=f"{value.normalize():f}")


def status_name(status: int) -> str:
    return OrderStatus.Name(status).removeprefix("ORDER_STATUS_")


def type_name(value: int) -> str:
    return orders_pb2.OrderType.Name(value).removeprefix("ORDER_TYPE_")


def is_active(state) -> bool:
    return state.status != OrderStatus.ORDER_STATUS_UNSPECIFIED and state.status not in FINAL_STATUSES


def executed(state) -> Decimal:
    return dec(state.executed_quantity) or Decimal(0) if state.HasField("executed_quantity") else Decimal(0)


def describe(state, names: Instruments | None = None) -> str:
    """Заявка для отчёта: имя, тип, сторона, количество, цены, статус, ключ, время. Без символа и счёта."""
    order = state.order
    name = names.name_of(order.symbol) if names is not None and order.symbol else "инструмент не указан"
    parts = [f"{name} {type_name(order.type)} {SIDE_NAMES.get(order.side, 'сторона ?')} {num(dec(order.quantity))}"]
    if order.HasField("limit_price"):
        parts.append(f"лимит {num(dec(order.limit_price))}")
    if order.HasField("stop_price"):
        condition = orders_pb2.StopCondition.Name(order.stop_condition).removeprefix("STOP_CONDITION_")
        parts.append(f"стоп {num(dec(order.stop_price))} {condition}")
    if order.valid_before:
        parts.append(orders_pb2.ValidBefore.Name(order.valid_before).removeprefix("VALID_BEFORE_"))
    parts.append(f"статус {status_name(state.status)}")
    if state.HasField("status_description") and state.status_description.value:
        parts.append(f"«{state.status_description.value}»")
    parts.append(f"номер {state.order_id or 'нет'}")
    parts.append(f"ключ {order.client_order_id or 'нет'}")
    if state.HasField("initial_quantity"):
        parts.append(f"объём {num(dec(state.initial_quantity))}, исполнено {num(executed(state))}")
    if state.triggered_order_id:
        parts.append(f"порождённая {state.triggered_order_id}")
    parts.append(f"выставлена {ts(state.transact_at)}")
    if state.HasField("withdraw_at"):
        parts.append(f"снята {ts(state.withdraw_at)}")
    return ", ".join(parts)


class OrderDesk:
    """Вызовы заявок одного счёта через шлюз."""

    def __init__(self, gateway: FinamGateway, account_id: str):
        self.gateway = gateway
        self.account_id = account_id

    def place(self, symbol: str, *, side: int, kind: int, quantity: Decimal, key: str,
              limit_price: Decimal | None = None, stop_price: Decimal | None = None, stop_condition: int = 0,
              valid_before: int = 0, time_in_force: int = 0):
        order = orders_pb2.Order(account_id=self.account_id, symbol=symbol, quantity=decimal(quantity), side=side,
                                 type=kind, time_in_force=time_in_force, client_order_id=key,
                                 stop_condition=stop_condition, valid_before=valid_before)
        if limit_price is not None:
            order.limit_price.CopyFrom(decimal(limit_price))
        if stop_price is not None:
            order.stop_price.CopyFrom(decimal(stop_price))
        return self.gateway.call("orders.PlaceOrder", order)

    def orders(self) -> list:
        return list(self.gateway.call("orders.GetOrders", orders_pb2.OrdersRequest(account_id=self.account_id)).orders)

    def get(self, order_id: str):
        return self.gateway.call("orders.GetOrder", orders_pb2.GetOrderRequest(account_id=self.account_id,
                                                                             order_id=order_id))

    def cancel(self, order_id: str):
        return self.gateway.call("orders.CancelOrder", orders_pb2.CancelOrderRequest(account_id=self.account_id,
                                                                                    order_id=order_id))

    def by_key(self, key: str) -> list:
        return [state for state in self.orders() if state.order.client_order_id == key]

    def cancel_probe_orders(self, symbol: str | None = None) -> tuple[list, list]:
        """Снять активные заявки разведки (ключ ``fp…``); вернуть снятые и отказы."""
        cancelled, failed = [], []
        for state in self.orders():
            order = state.order
            if not (is_active(state) and order.client_order_id.startswith(KEY_PREFIX)):
                continue
            if symbol is not None and order.symbol != symbol:
                continue
            try:
                cancelled.append(self.cancel(state.order_id))
            except BrokerCallError as exc:
                failed.append((state, exc))
        return cancelled, failed


class Streams:
    """``SubscribeOrders``, ``SubscribeTrades`` и устаревший ``SubscribeOrderTrade`` по счёту."""

    NAMES = ("SubscribeOrders", "SubscribeTrades", "SubscribeOrderTrade")

    def __init__(self, gateway: FinamGateway, account_id: str):
        self.gateway = gateway
        self.account_id = account_id
        self.handles: dict[str, StreamHandle] = {}

    def open(self) -> None:
        account = self.account_id
        self.handles["SubscribeOrders"] = self.gateway.open_stream(
            "orders.SubscribeOrders", orders_pb2.SubscribeOrdersRequest(account_id=account))
        self.handles["SubscribeTrades"] = self.gateway.open_stream(
            "orders.SubscribeTrades", orders_pb2.SubscribeTradesRequest(account_id=account))
        self.handles["SubscribeOrderTrade"] = self.gateway.open_stream(
            "orders.SubscribeOrderTrade", iterate([orders_pb2.OrderTradeRequest(
                action=orders_pb2.OrderTradeRequest.ACTION_SUBSCRIBE,
                data_type=orders_pb2.OrderTradeRequest.DATA_TYPE_ALL, account_id=account)]))

    def state(self) -> str:
        parts = []
        for name in self.NAMES:
            handle = self.handles.get(name)
            if handle is None:
                parts.append(f"{name}: не открыт")
            elif handle.error is not None:
                parts.append(f"{name}: закрыт с ошибкой {handle.error}")
            elif handle.closed.is_set():
                parts.append(f"{name}: закрыт сервером")
            else:
                parts.append(f"{name}: открыт, сообщений {len(handle.snapshot())}")
        return "; ".join(parts)

    def order_events(self, match: Callable) -> dict[str, list]:
        """Состояния заявок из стримов заявок, подходящие под ``match``, по стримам, в порядке прихода."""
        found: dict[str, list] = {}
        for name in ("SubscribeOrders", "SubscribeOrderTrade"):
            handle = self.handles.get(name)
            if handle is None:
                continue
            found[name] = [(moment, state) for moment, message in handle.snapshot()
                           for state in message.orders if match(state)]
        return found

    def trade_events(self, match: Callable) -> dict[str, list]:
        found: dict[str, list] = {}
        for name in ("SubscribeTrades", "SubscribeOrderTrade"):
            handle = self.handles.get(name)
            if handle is None:
                continue
            found[name] = [(moment, trade) for moment, message in handle.snapshot()
                           for trade in message.trades if match(trade)]
        return found

    def wait_order_event(self, match: Callable, timeout: float, sleep) -> bool:
        handle = self.handles.get("SubscribeOrders")
        other = self.handles.get("SubscribeOrderTrade")
        waited, step = 0.0, 0.5
        while True:
            if any(events for events in self.order_events(match).values()):
                return True
            if waited >= timeout or all(h is None or h.closed.is_set() for h in (handle, other)):
                return any(events for events in self.order_events(match).values())
            sleep(step)
            waited += step

    def wait_trade_event(self, match: Callable, timeout: float, sleep) -> bool:
        waited, step = 0.0, 0.5
        while True:
            if any(events for events in self.trade_events(match).values()):
                return True
            if waited >= timeout:
                return False
            sleep(step)
            waited += step

    def close(self) -> None:
        for handle in self.handles.values():
            handle.cancel()


def events_summary(events: dict[str, list], reference_id: str) -> str:
    """По стримам: число событий, номер в первом событии против номера из ответа, статусы по порядку."""
    parts = []
    for name, items in events.items():
        if not items:
            parts.append(f"{name}: событий нет")
            continue
        first = items[0][1]
        same = "совпал" if first.order_id == reference_id else f"другой ({first.order_id or 'пусто'})"
        statuses = " → ".join(status_name(state.status) for _, state in items)
        parts.append(f"{name}: {len(items)} событий, номер в первом {same}, статусы {statuses}")
    return "; ".join(parts)


def cleanup_orders(gateway: FinamGateway, account_id: str, out: Callable[[str], None],
                   names: Instruments | None = None) -> int:
    """Действие ``cleanup``: снять только заявки разведки, чужие не трогать, позиции не закрывать."""
    desk = OrderDesk(gateway, account_id)
    states = desk.orders()
    foreign = [state for state in states if is_active(state) and not state.order.client_order_id.startswith(KEY_PREFIX)]
    cancelled, failed = desk.cancel_probe_orders()
    out(f"Счёт {mask_account(account_id)}: снято заявок разведки: {len(cancelled)}")
    for state in cancelled:
        out(f"  {gateway.describe(describe(state, names))}")
    for state, exc in failed:
        out(f"  не снята: {gateway.describe(describe(state, names))} — {exc}")
    out(f"Чужих активных заявок: {len(foreign)} (не тронуты). Позиции не закрываются.")
    return 1 if failed else 0
