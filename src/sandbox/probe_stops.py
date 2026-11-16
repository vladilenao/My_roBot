"""Разведка стоп-заявок песочницы: проверки П19–П24 на двух лотах (add-broker-protection, решение 11).

П25–П26 (add-account-vs-emulation-report, решение 7) тем же прогоном проверяют,
как песочница отдаёт комиссию покупки в операциях: есть ли в операции номера
сделок биржи и связана ли с ней операция «комиссия брокера».

Отвечает на вопросы, от которых зависит стоп у брокера в контуре «Песочница»:
в чём цена стоп-заявки в ``GetStopOrders``, что отвечает повтор ключа, как
снять и поставить стоп заново, что приносит стрим по заявке, порождённой
стопом, находит ли её ``GetOrderState`` по номеру биржи и что отвечает
снятие исполненной стоп-заявки. Подготовка (счёт, торги, стрим, покупка) и
уборка — как у ``probe``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from t_tech.invest.schemas import (
    GetOperationsByCursorRequest,
    OperationState,
    OperationType,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderIdType,
    OrderType,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderStatusOption,
    StopOrderType,
)
from t_tech.invest.utils import decimal_to_quotation, money_to_decimal

from src.sandbox.actions import STATUS_NAMES, active_stop_orders, describe_fill, new_key, price, rub
from src.sandbox.gateway import BrokerCallError
from src.sandbox.probe import NO, SKIPPED, STOP_TOUCH_GRACE, YES, Probe, floor_to_step

LOTS = 2
STOP_CHECKS = (
    ("П1", "Счёт робота в песочнице"),
    ("П3", "Инструмент и торги"),
    ("П5", "Стрим исполнений"),
    ("П10", "Покупка 2 лотов по рынку"),
    ("П25", "Номер сделки в операции"),
    ("П26", "Комиссия связана с операцией"),
    ("П19", "Стоп-заявка на объём"),
    ("П20", "Повтор ключа стоп-заявки"),
    ("П21", "Снять и поставить"),
    ("П22", "Срабатывание и стрим"),
    ("П23", "Заявка стопа по номеру биржи"),
    ("П24", "Снятие исполненной"),
    ("П15", "Песочница убрана"),
)
STOP_STATUS = {
    StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE: "активна",
    StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED: "исполнена",
    StopOrderStatusOption.STOP_ORDER_STATUS_CANCELED: "снята",
    StopOrderStatusOption.STOP_ORDER_STATUS_EXPIRED: "истекла",
}


def _tail(value: str | None) -> str:
    return f"…{value[-4:]}" if value else "нет"


class StopProbe(Probe):
    CHECKS = STOP_CHECKS
    TITLE = "Разведка стоп-заявок песочницы"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buy_id = ""
        self._first_key = self._first_id = ""
        self._second_id = ""
        self._exchange_id = ""

    def _params_line(self) -> str:
        return (f"Параметры: инструмент {self.settings.instrument}, --stop-ticks {self.settings.stop_ticks}, "
                f"--stop-wait {self.settings.stop_wait_min:g}, лотов {LOTS}.")

    # --- ход разведки ----------------------------------------------------
    def _run(self) -> None:
        codes = [code for code, _ in STOP_CHECKS if code != "П15"]
        if not self._step("П1", self._check_account):
            self.exit_code = 1
            self._skip(codes[1:], "нет счёта робота")
            return
        if not self._step("П3", self._check_instrument):
            if self.exit_code == 0:
                self.exit_code = 1
            self._skip(codes[2:], "торги не идут" if self.results["П3"].outcome == NO
                       else "инструмент не определён")
            return
        self._step("П5", self._start_stream)
        if not self._step("П10", self._buy):
            self._skip(codes[4:], "позиции нет")
            return
        self._step("П25", self._check_fee_operations)
        self._skip(["П26"], "проверка операций прервана")
        if not self._step("П19", self._place_first):
            self._skip(codes[codes.index("П20"):], "стоп-заявки нет")
            return
        self._step("П20", self._repeat_key)
        if not self._step("П21", self._replace):
            self._skip(codes[codes.index("П22"):], "новой стоп-заявки нет")
            return
        if not self._step("П22", self._wait_trigger):
            self._skip(["П23", "П24"], "стоп-заявка не исполнена")
            return
        self._step("П23", self._order_by_exchange_id)
        self._step("П24", self._cancel_executed)

    # --- запросы ---------------------------------------------------------
    def _post_stop(self, key: str):
        return self.gateway.call(
            "stop_orders.post_stop_order", instrument_id=self.instrument.uid, quantity=LOTS,
            stop_price=decimal_to_quotation(self._stop_price), direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
            account_id=self.account.account_id,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS, order_id=key)

    def _all_stops(self) -> list:
        return list(self.gateway.call("stop_orders.get_stop_orders", account_id=self.account.account_id,
                                      status=StopOrderStatusOption.STOP_ORDER_STATUS_ALL,
                                      from_=self.started_at - timedelta(minutes=1),
                                      to=self.timing.clock() + timedelta(minutes=1)).stop_orders)

    def _stop(self, stop_id: str):
        return next((stop for stop in self._all_stops() if stop.stop_order_id == stop_id), None)

    def _own_active(self) -> list:
        return [stop for stop in active_stop_orders(self.gateway, self.account)
                if stop.instrument_uid == self.instrument.uid]

    def _price_units(self, stop) -> str:
        """В чём брокер отдаёт стоп-цену: пункты или рубли за контракт (через стоимость шага)."""
        value = money_to_decimal(stop.stop_price)
        currency = stop.stop_price.currency or "без валюты"
        shown = f"стоп-цена в GetStopOrders {value.normalize():f} ({currency}) при стопе {price(self._stop_price)}"
        step, cost = self.instrument.price_step, self.instrument.step_cost
        if abs(value - self._stop_price) < step / 2:
            return f"{shown}: пункты"
        if cost:
            in_rub = self._stop_price / step * cost
            if abs(value - in_rub) <= in_rub * Decimal("0.01"):
                return f"{shown}: рубли за контракт (стоимость шага {cost})"
        return f"{shown}: единицы не распознаны"

    # --- П10: позиция ----------------------------------------------------
    def _buy(self) -> bool:
        response = self.gateway.call(
            "orders.post_order", instrument_id=self.instrument.uid, quantity=LOTS,
            direction=OrderDirection.ORDER_DIRECTION_BUY, account_id=self.account.account_id,
            order_type=OrderType.ORDER_TYPE_MARKET, order_id=new_key())
        self._placed = True
        self._buy_id = response.order_id
        lots = response.lots_executed
        for _ in range(int(self.timing.event_wait_secs // max(self.timing.poll_secs, 1)) + 1):
            if lots >= LOTS:
                break
            self.timing.sleep(self.timing.poll_secs)
            lots = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                     order_id=response.order_id).lots_executed
        detail = describe_fill(response)
        held = self._instrument_lots()
        if lots >= LOTS and held >= LOTS:
            self._set("П10", YES, f"{detail}; позиция {held} лот(а)")
            return True
        self._set("П10", NO, f"{detail}; исполнено {lots} из {LOTS}, позиция {held}")
        return False

    # --- П25–П26: комиссия в операциях --------------------------------------
    def _buy_trade_ids(self) -> set[str]:
        """Номера сделок биржи покупки: из стрима и из ``GetOrderState.stages``."""
        ids = {trade.trade_id for item in self._stream.seen if item.order_id == self._buy_id
               for trade in (item.trades or ()) if trade.trade_id}
        try:
            state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                      order_id=self._buy_id)
            ids |= {stage.trade_id for stage in (state.stages or ()) if stage.trade_id}
        except BrokerCallError:
            pass
        return ids

    def _operations(self) -> list:
        response = self.gateway.call("operations.get_operations_by_cursor", GetOperationsByCursorRequest(
            account_id=self.account.account_id, instrument_id=self.instrument.uid,
            from_=self.started_at - timedelta(minutes=1), to=self.timing.clock() + timedelta(minutes=1)))
        return [item for item in response.items if item.state == OperationState.OPERATION_STATE_EXECUTED]

    def _check_fee_operations(self) -> bool:
        """П25 и П26: опрос операций раз в ``poll_secs`` до ``operations_wait_secs``."""
        trade_ids = self._buy_trade_ids()
        waited = 0.0
        buy = fee = None
        while True:
            items = self._operations()
            buys = [item for item in items if item.type == OperationType.OPERATION_TYPE_BUY]
            buy = next((item for item in buys if trade_ids & self._operation_trades(item)), None) \
                or (buys[-1] if buys else None)
            if buy is not None:
                fee = next((item for item in items if item.type == OperationType.OPERATION_TYPE_BROKER_FEE
                            and item.parent_operation_id and item.parent_operation_id == buy.id), None)
            if fee is not None or waited >= self.timing.operations_wait_secs:
                break
            self.timing.sleep(self.timing.poll_secs)
            waited += self.timing.poll_secs
        if buy is None:
            self._set("П25", NO, f"операции покупки не видно за {self.timing.operations_wait_secs:g} с")
            self._set("П26", NO, f"операции не видно за {self.timing.operations_wait_secs:g} с")
            return False
        found = trade_ids & self._operation_trades(buy)
        listed = len(self._operation_trades(buy))
        detail = (f"в операции покупки номеров сделок {listed}, из них сделок покупки из стрима и stages "
                  f"{len(found)} из {len(trade_ids)}")
        self._set("П25", YES if trade_ids and found == trade_ids else NO, detail)
        self._set_fee_link(buy, fee, waited, items)
        return True

    @staticmethod
    def _operation_trades(item) -> set[str]:
        trades = getattr(getattr(item, "trades_info", None), "trades", None)
        return {trade.num for trade in trades if trade.num} if isinstance(trades, list) else set()

    def _set_fee_link(self, buy, fee, waited: float, items: list) -> None:
        field = abs(money_to_decimal(buy.commission)) if buy.commission and buy.commission.currency else None
        field_text = f"поле commission покупки {rub(field)}" if field is not None else "поля commission у покупки нет"
        if fee is None:
            loose = [item for item in items if item.type == OperationType.OPERATION_TYPE_BROKER_FEE]
            other = (f"; операций «комиссия брокера» без ссылки на покупку: {len(loose)}" if loose else "")
            self._set("П26", NO, f"«комиссии брокера» со ссылкой на покупку нет за "
                                 f"{self.timing.operations_wait_secs:g} с; {field_text}{other}")
            return
        amount = abs(money_to_decimal(fee.payment))
        same = ("совпадают" if field is not None and field == amount
                else "не совпадают" if field is not None else "сравнить не с чем")
        children = getattr(buy, "child_operations", None)
        children = len(children) if isinstance(children, list) else 0
        self._set("П26", YES, f"«комиссия брокера» {rub(amount)} со ссылкой на покупку через {waited:g} с; "
                              f"{field_text}, {same}; дочерних операций у покупки {children}")

    # --- П19–П21 ---------------------------------------------------------
    def _place_first(self) -> bool:
        last = self._last_price()
        self._stop_price = floor_to_step(last - self.settings.stop_ticks * self.instrument.price_step,
                                         self.instrument.price_step)
        self._first_key = new_key()
        response = self._post_stop(self._first_key)
        self._first_id = response.stop_order_id
        echo = "ключ в ответе совпал" if getattr(response, "order_request_id", "") == self._first_key \
            else "ключа в ответе нет"
        ours = next((stop for stop in self._own_active() if stop.stop_order_id == self._first_id), None)
        head = f"стоп-заявка {_tail(self._first_id)} (последняя {price(last)}), {echo}"
        if ours is None:
            self._set("П19", NO, f"{head}; в GetStopOrders её нет")
            return False
        detail = f"{head}; лотов {ours.lots_requested}; {self._price_units(ours)}"
        self._set("П19", YES if ours.lots_requested == LOTS else NO, detail)
        return True

    def _repeat_key(self) -> bool:
        try:
            response = self._post_stop(self._first_key)
            answer = ("брокер вернул ту же стоп-заявку" if response.stop_order_id == self._first_id
                      else f"брокер вернул новую стоп-заявку {_tail(response.stop_order_id)}")
        except BrokerCallError as exc:
            answer = f"брокер ответил ошибкой {exc}"
        active = self._own_active()
        extra = [stop for stop in active if stop.stop_order_id != self._first_id]
        for stop in extra:   # для П21 нужна одна стоп-заявка
            self.gateway.call("stop_orders.cancel_stop_order", account_id=self.account.account_id,
                              stop_order_id=stop.stop_order_id)
        if len(active) == 1:
            self._set("П20", YES, f"{answer}; активная стоп-заявка одна")
            return True
        self._set("П20", NO, f"{answer}; активных стоп-заявок {len(active)}, лишние сняты")
        return False

    def _replace(self) -> bool:
        self.gateway.call("stop_orders.cancel_stop_order", account_id=self.account.account_id,
                          stop_order_id=self._first_id)
        first = self._stop(self._first_id)
        status = STOP_STATUS.get(first.status, first.status.name) if first is not None else "не найдена"
        if first is None or first.status != StopOrderStatusOption.STOP_ORDER_STATUS_CANCELED:
            self._set("П21", NO, f"после снятия стоп-заявка {_tail(self._first_id)}: {status}")
            return False
        response = self._post_stop(new_key())
        self._second_id = response.stop_order_id
        active = [stop.stop_order_id for stop in self._own_active()]
        if active == [self._second_id]:
            self._set("П21", YES, f"{_tail(self._first_id)} снята, новая {_tail(self._second_id)} на {LOTS} лота "
                                  f"активна, других нет")
            return True
        self._set("П21", NO, f"{_tail(self._first_id)} снята; активные после постановки: "
                             f"{', '.join(_tail(i) for i in active) or 'нет'}")
        return False

    # --- П22–П24 ---------------------------------------------------------
    def _wait_trigger(self) -> bool:
        deadline = self.timing.clock() + timedelta(minutes=self.settings.stop_wait_min)
        lowest: Decimal | None = None
        touched_at: datetime | None = None
        while True:
            stop = self._stop(self._second_id)
            if stop is not None and stop.status == StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED:
                return self._triggered(stop, touched_at, self.timing.clock())
            last = self._last_price()
            lowest = last if lowest is None else min(lowest, last)
            now = self.timing.clock()
            if last <= self._stop_price and touched_at is None:
                touched_at = now
            if touched_at is not None and now - touched_at >= STOP_TOUCH_GRACE and stop is not None \
                    and stop.status == StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE:
                self._set("П22", NO, f"цена коснулась стопа {price(self._stop_price)} в {touched_at:%H:%M:%S} UTC, "
                                     f"через минуту стоп-заявка активна")
                return False
            if now >= deadline:
                self._set("П22", SKIPPED, f"цена до стопа {price(self._stop_price)} не дошла за "
                                          f"{self.settings.stop_wait_min:g} мин, минимум {price(lowest)}")
                return False
            self.timing.sleep(self.timing.poll_secs)

    def _triggered(self, stop, touched_at: datetime | None, executed_at: datetime) -> bool:
        self._exchange_id = stop.exchange_order_id or ""
        delay = (f"исполнение замечено через {(executed_at - touched_at).total_seconds():g} с после касания"
                 if touched_at is not None else "касание опросом не замечено")
        parts = [f"стоп-заявка исполнена, exchange_order_id {_tail(self._exchange_id)}", delay]
        if self._stream.thread is None:
            self._set("П22", NO, "; ".join(parts + ["стрим не подключён"]))
            return True
        known = {self._buy_id}
        event = self._wait_event(lambda item: bool(self._exchange_id) and item.order_id == self._exchange_id
                                 and item.lots_executed > 0)
        strangers = [item for item in self._stream.seen
                     if item.instrument_uid == self.instrument.uid and item.order_id not in known]
        if event is None:
            seen = ", ".join(f"{_tail(item.order_id)} ({STATUS_NAMES.get(item.execution_report_status, '?')})"
                             for item in strangers) or "нет"
            self._set("П22", NO, "; ".join(parts + [f"в стриме нет отчёта по {_tail(self._exchange_id)} за "
                                                    f"{self.timing.event_wait_secs:g} с; незнакомые отчёты: {seen}"]))
            return True
        request_id = getattr(event, "order_request_id", "") or ""
        temporary = sorted({item.order_id for item in strangers} - {self._exchange_id})
        parts += [
            "order_request_id в отчёте " + ("есть" if request_id else "пуст"),
            "номер отчёта совпадает с exchange_order_id",
            f"исполнено {event.lots_executed} из {event.lots_requested}, сделок {len(list(event.trades or ()))}",
        ]
        if temporary:
            parts.append("до него отчёты с другими номерами: " + ", ".join(_tail(i) for i in temporary))
        self._set("П22", YES, "; ".join(parts))
        return True

    def _order_by_exchange_id(self) -> bool:
        if not self._exchange_id:
            self._set("П23", NO, "у исполненной стоп-заявки нет exchange_order_id")
            return False
        try:
            state = self.gateway.call("orders.get_order_state", account_id=self.account.account_id,
                                      order_id=self._exchange_id, order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE)
        except BrokerCallError as exc:
            self._set("П23", NO, f"заявка {_tail(self._exchange_id)} не найдена: {exc}")
            return False
        status = STATUS_NAMES.get(state.execution_report_status, state.execution_report_status.name)
        stages = list(state.stages or ())
        total = money_to_decimal(state.executed_order_price)
        detail = (f"{status}, исполнено {state.lots_executed} из {state.lots_requested}, сделок в stages "
                  f"{len(stages)}, сумма исполнения {total.normalize():f} "
                  f"({state.executed_order_price.currency or 'без валюты'}), order_request_id "
                  + ("есть" if state.order_request_id else "пуст"))
        filled = state.execution_report_status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
        self._set("П23", YES if filled or state.lots_executed else NO, detail)
        return True

    def _cancel_executed(self) -> bool:
        try:
            self.gateway.call("stop_orders.cancel_stop_order", account_id=self.account.account_id,
                              stop_order_id=self._second_id)
            answer = "брокер ответил успехом"
        except BrokerCallError as exc:
            answer = f"брокер ответил ошибкой {exc}"
        stop = self._stop(self._second_id)
        status = STOP_STATUS.get(stop.status, stop.status.name) if stop is not None else "не найдена"
        self._set("П24", YES, f"{answer}; статус в GetStopOrders: {status}")
        return True
