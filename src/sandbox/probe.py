"""Разведка песочницы: проверки П1–П15 на одном инструменте и отчёт.

Каждая проверка получает итог «да», «нет», «не проверено» или «ошибка».
«Нет» — это ответ разведки, а не сбой: проверки, которые от неё не зависят,
продолжаются. Если разведка поставила хотя бы одну заявку, П15 убирает
инструмент в любом случае, даже после исключения (design.md, решение 4).
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from t_tech.invest.schemas import (
    GetMaxLotsRequest,
    GetOperationsByCursorRequest,
    OperationType,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderIdType,
    OrderStateStreamRequest,
    OrderType,
    ResultSubscriptionStatus,
    SecurityTradingStatus,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderStatusOption,
    StopOrderType,
)
from t_tech.invest.utils import decimal_to_money, decimal_to_quotation, money_to_decimal, quotation_to_decimal

from src.logging_setup import get_logger
from src.sandbox.account import AccountConflict, RobotAccount, robot_account
from src.sandbox.actions import (
    ACTIVE_STATUSES,
    STATUS_NAMES,
    active_orders,
    active_stop_orders,
    describe_fill,
    new_key,
    open_positions,
    price,
    rub,
    rub_balance,
)
from src.sandbox.gateway import BrokerCallError, SandboxGateway
from src.sandbox.instruments import InstrumentNameError, SandboxInstrument, resolve_instrument

log = get_logger(__name__)

YES, NO, SKIPPED, ERROR = "да", "нет", "не проверено", "ошибка"
CHECKS = (
    ("П1", "Счёт робота в песочнице"),
    ("П2", "Тариф и лимиты"),
    ("П3", "Инструмент и торги"),
    ("П4", "GetMaxLots"),
    ("П5", "Стрим исполнений"),
    ("П6", "Лимитная заявка"),
    ("П7", "Поиск по ключу"),
    ("П8", "Повтор ключа"),
    ("П9", "Отмена"),
    ("П10", "Рыночная покупка"),
    ("П11", "Позиция и капитал"),
    ("П12", "Операции"),
    ("П13", "Стоп-заявка ставится"),
    ("П14", "Стоп-заявка исполняется"),
    ("П15", "Песочница убрана"),
)
DEFAULT_INSTRUMENT = "NG"
LIMIT_DISCOUNT = Decimal("0.05")
STREAM_READY_SECS = 10
STOP_TOUCH_GRACE = timedelta(minutes=1)
# Отказ по существу заявки, а не сбой сети или лимит запросов: только тогда
# имеет смысл подсказка повторить разведку на акции.
REJECTION_CODES = frozenset({"INVALID_ARGUMENT", "FAILED_PRECONDITION", "PERMISSION_DENIED", "NOT_FOUND"})


@dataclass
class CheckResult:
    code: str
    title: str
    outcome: str = SKIPPED
    detail: str = ""


@dataclass
class ProbeSettings:
    instrument: str = DEFAULT_INSTRUMENT
    stop_ticks: int = 2
    stop_wait_min: float = 10
    min_rub: int = 1_000_000


@dataclass
class ProbeTiming:
    """Часы и паузы разведки; тесты подменяют их, чтобы не ждать минутами."""

    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleep: Callable[[float], None] = time.sleep
    poll_secs: float = 10
    event_wait_secs: float = 30
    operations_wait_secs: float = 60
    stream_ready_secs: float = STREAM_READY_SECS


def _present(value) -> bool:
    """Поле oneof задано: SDK оставляет у незаданного поля заглушку, а не сообщение."""
    return value is not None and dataclasses.is_dataclass(value)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass
class _StreamState:
    events: "queue.Queue" = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    seen: list = field(default_factory=list)
    error: BrokerCallError | None = None
    thread: threading.Thread | None = None


class Probe:
    def __init__(self, gateway: SandboxGateway, settings: ProbeSettings, *, timing: ProbeTiming | None = None,
                 resolve: Callable[..., SandboxInstrument] = resolve_instrument):
        self.gateway = gateway
        self.settings = settings
        self.timing = timing or ProbeTiming()
        self._resolve = resolve
        self.results = {code: CheckResult(code, title) for code, title in CHECKS}
        self.account: RobotAccount | None = None
        self.instrument: SandboxInstrument | None = None
        self.started_at = self.timing.clock()
        self.finished_at: datetime | None = None
        self.exit_code = 0
        self._placed = False
        self._stream = _StreamState()
        self._limit_key = self._limit_id = ""
        self._stop_id = ""
        self._stop_price = Decimal(0)

    # --- итоги -----------------------------------------------------------
    def _set(self, code: str, outcome: str, detail: str = "") -> None:
        self.results[code].outcome = outcome
        self.results[code].detail = detail

    def _skip(self, codes, reason: str) -> None:
        for code in codes:
            if self.results[code].outcome == SKIPPED and not self.results[code].detail:
                self._set(code, SKIPPED, reason)

    def _step(self, code: str, fn: Callable[[], bool | None]) -> bool:
        """Проверка: ``BrokerCallError`` и любой сбой → «ошибка», разведка продолжается."""
        try:
            return bool(fn())
        except BrokerCallError as exc:
            self._set(code, ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 — отчёт важнее падения
            log.error("Разведка %s: сбой %s", code, type(exc).__name__)
            self._set(code, ERROR, f"сбой {type(exc).__name__}")
        return False

    # --- ход разведки ----------------------------------------------------
    def run(self) -> int:
        try:
            self._run()
        finally:
            if self._placed:
                self._step("П15", self._cleanup)
            else:
                self._skip(["П15"], "заявок не было")
            self.finished_at = self.timing.clock()
        return self.exit_code

    def _run(self) -> None:
        all_codes = [code for code, _ in CHECKS]
        if not self._step("П1", self._check_account):
            self.exit_code = 1
            self._skip(all_codes[1:], "нет счёта робота")
            return
        self._step("П2", self._check_tariff)
        if not self._step("П3", self._check_instrument):
            if self.exit_code == 0:
                self.exit_code = 1
            self._skip(all_codes[3:], "торги не идут" if self.results["П3"].outcome == NO
                       else "инструмент не определён")
            return
        self._step("П4", self._check_max_lots)
        self._step("П5", self._start_stream)
        if self._step("П6", self._place_limit):
            self._step("П7", self._find_by_key)
            self._step("П8", self._repeat_key)
            self._step("П9", self._cancel_limit)
        else:
            self._skip(["П7", "П8", "П9"], "лимитной заявки нет")
        if not self._step("П10", self._market_buy):
            self._skip(["П11", "П12", "П13", "П14"], "позиции нет")
            return
        self._step("П11", self._check_position)
        self._step("П12", self._check_operations)
        if self._step("П13", self._place_stop):
            self._step("П14", self._wait_stop)
        else:
            self._skip(["П14"], "стоп-заявки нет")

    # --- П1–П4 -----------------------------------------------------------
    def _check_account(self) -> bool:
        try:
            self.account = robot_account(self.gateway)
        except AccountConflict as exc:
            self._set("П1", ERROR, str(exc))
            return False
        balance = rub_balance(self.gateway, self.account)
        detail = f"счёт {self.account.masked}" + (", открыт сейчас" if self.account.opened else "")
        if balance < self.settings.min_rub:
            amount = Decimal(self.settings.min_rub) - balance
            self.gateway.call("sandbox.sandbox_pay_in", account_id=self.account.account_id,
                              amount=decimal_to_money(amount, "rub"))
            detail += f", пополнен на {rub(amount)} до {rub(Decimal(self.settings.min_rub))}"
        else:
            detail += f", на счёте {rub(balance)}"
        self._set("П1", YES, detail)
        return True

    def _check_tariff(self) -> bool:
        tariff = self.gateway.call("users.get_user_tariff")
        services: dict[str, int] = {}
        for limit in tariff.unary_limits:
            for method in limit.methods:
                service = method.rsplit(".", 1)[-1].split("/")[0]
                if service in ("OrdersService", "StopOrdersService", "OperationsService", "SandboxService"):
                    services[service] = max(services.get(service, 0), limit.limit_per_minute)
        streams = []
        for limit in tariff.stream_limits:
            names = ", ".join(sorted({stream.rsplit(".", 1)[-1].split("/")[-1] for stream in limit.streams}))
            streams.append(f"{names}: {limit.limit}")
        parts = [f"{name} {value}/мин" for name, value in sorted(services.items())]
        if streams:
            parts.append("стримы " + "; ".join(streams))
        self._set("П2", YES, "; ".join(parts) or "лимиты не названы")
        return True

    def _check_instrument(self) -> bool:
        try:
            self.instrument = self._resolve(self.gateway, self.settings.instrument)
        except InstrumentNameError as exc:
            self.exit_code = 2
            self._set("П3", ERROR, str(exc))
            return False
        item = self.instrument
        status = self.gateway.call("market_data.get_trading_status", instrument_id=item.uid)
        detail = f"{item.short_name}: лот {item.lot}, шаг {price(item.price_step)}"
        if item.step_cost is not None:
            detail += f", стоимость шага {rub(item.step_cost)}"
        if item.chosen:
            detail = f"{item.chosen}; {detail}"
        if status.trading_status != SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING:
            self._set("П3", NO, f"торги не идут ({status.trading_status.name}); {detail}")
            return False
        self._set("П3", YES, f"{detail}, нормальная торговля")
        return True

    def _check_max_lots(self) -> bool:
        response = self.gateway.call("orders.get_max_lots", GetMaxLotsRequest(
            account_id=self.account.account_id, instrument_id=self.instrument.uid))
        cash, margin = response.buy_limits, response.buy_margin_limits
        self._set("П4", YES, f"без плеча: до {cash.buy_max_lots} лотов, по рынку {cash.buy_max_market_lots}; "
                             f"с плечом: до {margin.buy_max_lots}, по рынку {margin.buy_max_market_lots}")
        return True

    # --- П5: стрим -------------------------------------------------------
    def _start_stream(self) -> bool:
        state = self._stream
        request = OrderStateStreamRequest(accounts=[self.account.account_id])

        def consume() -> None:
            try:
                for message in self.gateway.stream("orders_stream.order_state_stream", request=request):
                    if _present(getattr(message, "subscription", None)):
                        if message.subscription.status == ResultSubscriptionStatus.RESULT_SUBSCRIPTION_STATUS_OK:
                            state.ready.set()
                    elif _present(getattr(message, "ping", None)):
                        state.ready.set()
                    elif _present(getattr(message, "order_state", None)):
                        state.ready.set()
                        state.events.put(message.order_state)
            except BrokerCallError as exc:
                state.error = exc
            except Exception as exc:  # noqa: BLE001 — поток стрима не должен ронять разведку
                state.error = BrokerCallError("orders_stream.order_state_stream", type(exc).__name__)

        state.thread = threading.Thread(target=consume, name="sandbox-order-stream", daemon=True)
        state.thread.start()
        if state.ready.wait(self.timing.stream_ready_secs):
            self._set("П5", YES, "подписка OrderStateStream подтверждена")
            return True
        if state.error is not None:
            self._set("П5", ERROR, str(state.error))
        else:
            self._set("П5", NO, f"подписка не подтверждена за {self.timing.stream_ready_secs:g} с")
        return False

    def _wait_event(self, match: Callable[[object], bool]) -> object | None:
        state = self._stream
        for event in state.seen:
            if match(event):
                return event
        if state.thread is None:
            return None
        deadline = time.monotonic() + self.timing.event_wait_secs
        while (left := deadline - time.monotonic()) > 0:
            try:
                event = state.events.get(timeout=min(left, 1))
            except queue.Empty:
                if state.error is not None and state.events.empty():
                    return None
                continue
            state.seen.append(event)
            if match(event):
                return event
        return None

    # --- П6–П9: лимитная заявка ------------------------------------------
    def _last_price(self) -> Decimal:
        prices = self.gateway.call("market_data.get_last_prices", instrument_id=[self.instrument.uid]).last_prices
        return quotation_to_decimal(prices[0].price)

    def _place_limit(self) -> bool:
        step = self.instrument.price_step
        last = self._last_price()
        target = floor_to_step(last * (1 - LIMIT_DISCOUNT), step)
        book = self.gateway.call("market_data.get_order_book", instrument_id=self.instrument.uid, depth=1)
        floor = quotation_to_decimal(book.limit_down)
        if floor > 0:
            target = max(target, ceil_to_step(floor, step))
        self._limit_key = new_key()
        response = self.gateway.call(
            "orders.post_order", instrument_id=self.instrument.uid, quantity=1, price=decimal_to_quotation(target),
            direction=OrderDirection.ORDER_DIRECTION_BUY, account_id=self.account.account_id,
            order_type=OrderType.ORDER_TYPE_LIMIT, order_id=self._limit_key)
        self._placed = True
        self._limit_id = response.order_id
        status = STATUS_NAMES.get(response.execution_report_status, response.execution_report_status.name)
        detail = f"цена {price(target)} (последняя {price(last)}), заявка …{self._limit_id[-4:]}, {status}"
        if response.execution_report_status in ACTIVE_STATUSES:
            self._set("П6", YES, detail)
            return True
        self._set("П6", NO, detail)
        return False

    def _find_by_key(self) -> bool:
        state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                  order_id=self._limit_key, order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)
        if state.order_id == self._limit_id:
            self._set("П7", YES, "GetOrderState по ключу вернул ту же заявку")
            return True
        self._set("П7", NO, f"по ключу найдена другая заявка …{state.order_id[-4:]}")
        return False

    def _repeat_key(self) -> bool:
        try:
            response = self.gateway.call(
                "orders.post_order", instrument_id=self.instrument.uid, quantity=1,
                price=decimal_to_quotation(self._limit_price()), direction=OrderDirection.ORDER_DIRECTION_BUY,
                account_id=self.account.account_id, order_type=OrderType.ORDER_TYPE_LIMIT, order_id=self._limit_key)
            answer = ("брокер вернул ту же заявку" if response.order_id == self._limit_id
                      else f"брокер вернул новую заявку …{response.order_id[-4:]}")
        except BrokerCallError as exc:
            answer = f"брокер ответил ошибкой {exc}"
        count = sum(1 for order in active_orders(self.gateway, self.account)
                    if order.instrument_uid == self.instrument.uid)
        if count == 1:
            self._set("П8", YES, f"{answer}; активная заявка одна")
            return True
        self._set("П8", NO, f"{answer}; активных заявок {count}")
        return False

    def _limit_price(self) -> Decimal:
        state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                  order_id=self._limit_id)
        return money_to_decimal(state.initial_security_price)

    def _cancel_limit(self) -> bool:
        self.gateway.call("orders.cancel_order", account_id=self.account.account_id, order_id=self._limit_id)
        state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                  order_id=self._limit_id)
        cancelled = state.execution_report_status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED
        event = self._wait_event(lambda item: item.order_id == self._limit_id and item.execution_report_status
                                 == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED)
        status = STATUS_NAMES.get(state.execution_report_status, state.execution_report_status.name)
        if cancelled and event is not None:
            self._set("П9", YES, "заявка снята, событие в стриме пришло")
            return True
        stream = "событие в стриме пришло" if event is not None else \
            f"события в стриме нет за {self.timing.event_wait_secs:g} с"
        self._set("П9", NO, f"статус заявки: {status}; {stream}")
        return False

    # --- П10–П12: позиция ------------------------------------------------
    def _market_buy(self) -> bool:
        key = new_key()
        try:
            response = self.gateway.call(
                "orders.post_order", instrument_id=self.instrument.uid, quantity=1,
                direction=OrderDirection.ORDER_DIRECTION_BUY, account_id=self.account.account_id,
                order_type=OrderType.ORDER_TYPE_MARKET, order_id=key)
        except BrokerCallError as exc:
            detail = f"отказ брокера {exc}"
            if self.instrument.kind == "future" and exc.code in REJECTION_CODES:
                detail += "; повторите разведку на акции: python -m src.sandbox probe SBER"
            self._set("П10", NO, detail)
            return False
        self._placed = True
        filled = response.lots_executed >= 1
        if not filled:
            for _ in range(int(self.timing.event_wait_secs // max(self.timing.poll_secs, 1)) + 1):
                state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                          order_id=response.order_id)
                if state.lots_executed >= 1:
                    filled = True
                    break
                self.timing.sleep(self.timing.poll_secs)
        event = self._wait_event(lambda item: (item.order_id == response.order_id
                                               or getattr(item, "order_request_id", None) == key)
                                 and item.lots_executed >= 1)
        detail = describe_fill(response)
        if not filled:
            self._set("П10", NO, f"{detail}; за {self.timing.event_wait_secs:g} с не исполнена")
            return False
        if event is None:
            self._set("П10", NO, f"{detail}; события исполнения в стриме нет за {self.timing.event_wait_secs:g} с")
        else:
            self._set("П10", YES, f"{detail}; событие в стриме пришло")
        return True

    def _instrument_lots(self) -> int:
        return sum(lots for uid, lots, _ in open_positions(self.gateway, self.account) if uid == self.instrument.uid)

    def _check_position(self) -> bool:
        positions = self.gateway.call("operations.get_positions", account_id=self.account.account_id)
        held = [item.balance for item in list(positions.futures) + list(positions.securities)
                if item.instrument_uid == self.instrument.uid]
        lots = self._instrument_lots()
        try:
            margin = self.gateway.call("users.get_margin_attributes", account_id=self.account.account_id)
            capital = f"GetMarginAttributes: liquid_portfolio {rub(money_to_decimal(margin.liquid_portfolio))}"
        except BrokerCallError as exc:
            capital = f"GetMarginAttributes не работает: {exc}"
        detail = f"GetPositions: {held[0] if held else 0}, GetPortfolio: {lots} лот(а); {capital}"
        if held and lots == 1:
            self._set("П11", YES, detail)
            return True
        self._set("П11", NO, detail)
        return False

    def _check_operations(self) -> bool:
        waited = 0.0
        while True:
            items = self.gateway.call("operations.get_operations_by_cursor", GetOperationsByCursorRequest(
                account_id=self.account.account_id, instrument_id=self.instrument.uid,
                from_=self.started_at - timedelta(minutes=1), to=self.timing.clock() + timedelta(minutes=1))).items
            buys = [item for item in items if item.type == OperationType.OPERATION_TYPE_BUY]
            if buys:
                fees = [money_to_decimal(item.commission) for item in buys if money_to_decimal(item.commission)]
                fees += [abs(money_to_decimal(item.payment)) for item in items
                         if item.type == OperationType.OPERATION_TYPE_BROKER_FEE]
                fee = f"комиссия {rub(sum(fees, Decimal(0)))}" if fees else "комиссии в операциях нет"
                self._set("П12", YES, f"покупка видна через {waited:g} с, {fee}")
                return True
            if waited >= self.timing.operations_wait_secs:
                self._set("П12", NO, f"покупки в операциях нет за {self.timing.operations_wait_secs:g} с")
                return False
            self.timing.sleep(self.timing.poll_secs)
            waited += self.timing.poll_secs

    # --- П13–П14: стоп-заявка --------------------------------------------
    def _place_stop(self) -> bool:
        last = self._last_price()
        self._stop_price = floor_to_step(last - self.settings.stop_ticks * self.instrument.price_step,
                                         self.instrument.price_step)
        response = self.gateway.call(
            "stop_orders.post_stop_order", instrument_id=self.instrument.uid, quantity=1,
            stop_price=decimal_to_quotation(self._stop_price), direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
            account_id=self.account.account_id,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS, order_id=new_key())
        self._stop_id = response.stop_order_id
        visible = any(stop.stop_order_id == self._stop_id for stop in active_stop_orders(self.gateway, self.account))
        detail = f"стоп {price(self._stop_price)} (последняя {price(last)}), стоп-заявка …{self._stop_id[-4:]}"
        if visible:
            self._set("П13", YES, f"{detail}, видна в GetStopOrders")
            return True
        self._set("П13", NO, f"{detail}, в GetStopOrders её нет")
        return False

    def _stop_status(self):
        stops = self.gateway.call("stop_orders.get_stop_orders", account_id=self.account.account_id,
                                  status=StopOrderStatusOption.STOP_ORDER_STATUS_ALL,
                                  from_=self.started_at - timedelta(minutes=1),
                                  to=self.timing.clock() + timedelta(minutes=1)).stop_orders
        ours = next((stop for stop in stops if stop.stop_order_id == self._stop_id), None)
        return None if ours is None else ours.status

    def _wait_stop(self) -> bool:
        deadline = self.timing.clock() + timedelta(minutes=self.settings.stop_wait_min)
        lowest: Decimal | None = None
        touched_at: datetime | None = None
        while True:
            status = self._stop_status()
            if status == StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED or self._instrument_lots() == 0:
                self._set("П14", YES, f"стоп {price(self._stop_price)} исполнен песочницей")
                return True
            last = self._last_price()
            lowest = last if lowest is None else min(lowest, last)
            now = self.timing.clock()
            if last <= self._stop_price and touched_at is None:
                touched_at = now
            if touched_at is not None and now - touched_at >= STOP_TOUCH_GRACE \
                    and status == StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE:
                self._set("П14", NO, f"цена коснулась стопа {price(self._stop_price)} в "
                                     f"{touched_at:%H:%M:%S} UTC, через минуту стоп-заявка активна")
                return False
            if now >= deadline:
                self._set("П14", SKIPPED, f"цена до стопа {price(self._stop_price)} не дошла за "
                                          f"{self.settings.stop_wait_min:g} мин, минимум {price(lowest)}")
                return False
            self.timing.sleep(self.timing.poll_secs)

    # --- П15: уборка -----------------------------------------------------
    def _cleanup(self) -> bool:
        uid = self.instrument.uid
        problems = []
        for order in active_orders(self.gateway, self.account):
            if order.instrument_uid == uid:
                try:
                    self.gateway.call("orders.cancel_order", account_id=self.account.account_id,
                                      order_id=order.order_id)
                except BrokerCallError as exc:
                    problems.append(f"заявка не снята: {exc}")
        for stop in active_stop_orders(self.gateway, self.account):
            if stop.instrument_uid == uid:
                try:
                    self.gateway.call("stop_orders.cancel_stop_order", account_id=self.account.account_id,
                                      stop_order_id=stop.stop_order_id)
                except BrokerCallError as exc:
                    problems.append(f"стоп-заявка не снята: {exc}")
        lots = self._instrument_lots()
        if lots:
            direction = OrderDirection.ORDER_DIRECTION_SELL if lots > 0 else OrderDirection.ORDER_DIRECTION_BUY
            try:
                self.gateway.call("orders.post_order", instrument_id=uid, quantity=abs(lots), direction=direction,
                                  account_id=self.account.account_id, order_type=OrderType.ORDER_TYPE_MARKET,
                                  order_id=new_key())
            except BrokerCallError as exc:
                problems.append(f"позиция не закрыта: {exc}")
        left_orders = sum(1 for order in active_orders(self.gateway, self.account) if order.instrument_uid == uid)
        left_stops = sum(1 for stop in active_stop_orders(self.gateway, self.account) if stop.instrument_uid == uid)
        left_lots = self._instrument_lots()
        if not problems and not (left_orders or left_stops or left_lots):
            self._set("П15", YES, "позиций, заявок и стоп-заявок по инструменту нет")
            return True
        problems.append(f"осталось: позиция {left_lots}, заявок {left_orders}, стоп-заявок {left_stops}")
        self._set("П15", NO, "; ".join(problems))
        return False

    # --- отчёт -----------------------------------------------------------
    def report_lines(self) -> list[str]:
        name = self.instrument.short_name if self.instrument else self.settings.instrument
        finished = self.finished_at or self.timing.clock()
        lines = [
            f"# Разведка песочницы: {name}",
            "",
            f"Начало {self.started_at:%Y-%m-%d %H:%M:%S} UTC, конец {finished:%Y-%m-%d %H:%M:%S} UTC.",
            f"Параметры: инструмент {self.settings.instrument}, --stop-ticks {self.settings.stop_ticks}, "
            f"--stop-wait {self.settings.stop_wait_min:g}, --min-rub {self.settings.min_rub}.",
            "",
            "| № | Проверка | Итог | Подробность |",
            "|---|---|---|---|",
        ]
        for result in self.results.values():
            detail = result.detail.replace("|", "/")
            lines.append(f"| {result.code} | {result.title} | {result.outcome} | {detail} |")
        return lines

    def write_report(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "report.md"
        path.write_text("\n".join(self.report_lines()) + "\n", encoding="utf-8")
        return path
