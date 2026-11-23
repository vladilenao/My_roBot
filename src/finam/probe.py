"""``probe``: одна лимитная заявка далеко от рынка — ключ, поиск, повтор, стримы, снятие.

Проверки Ф0.10–Ф0.17 и уборка Ф0.99 (add-finam-probe, решение 5). Заявка на
покупку 1 лота ниже рынка на ``--offset-pct`` процентов живёт секунды и
исполниться не должна. Если всё же исполнилась — разведка останавливается и
сообщает о позиции, но не закрывает её: решает владелец.
"""

from __future__ import annotations

from decimal import Decimal

from src.finam.checks import ERROR, NO, SKIPPED, YES, Run, ProbeHalt, floor_to_step, new_key, num
from src.finam.instruments import FinamInstrument, dec
from src.finam.orders import (
    FILLED_STATUSES,
    OrderDesk,
    Streams,
    describe,
    events_summary,
    executed,
    is_active,
    status_name,
)
from src.finam.proto.grpc.tradeapi.v1 import side_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.sandbox.gateway import BrokerCallError

DEFAULT_INSTRUMENT = "NG"
# Отказы, при которых повтор с другим ключом или ценой бессмыслен: сеть, лимит, доступ.
_TRANSIENT = frozenset({"RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "UNAUTHENTICATED",
                        "PERMISSION_DENIED", "INTERNAL"})


def no_coverage(error: BrokerCallError) -> bool:
    """Отказ из-за нехватки денег под ГО: повтор с другим ключом или ценой ничего не даст (прогон 23.11, код [145])."""
    text = error.details.lower()
    return "[145]" in text or "coverage" in text or "обеспечени" in text


class OrderRun(Run):
    """Общее у действий с заявками: инструмент, торги, стримы, уборка заявок разведки."""

    PLACES_ORDERS = True
    PREPARE = "Ф0.10"

    def __init__(self, *args, instrument: str = DEFAULT_INSTRUMENT, **kwargs):
        super().__init__(*args, **kwargs)
        self.instrument_name = instrument
        self.item: FinamInstrument | None = None
        self.desk = OrderDesk(self.gateway, self.account_id)
        self.streams = Streams(self.gateway, self.account_id)
        self.last: Decimal | None = None
        self.step: Decimal | None = None
        self._keys: set = set()

    def prepare(self) -> bool:
        """Инструмент, торги и последняя цена; без торгов — ни одной заявки, код 1."""
        code = self.PREPARE
        self.item = self.resolve(self.instrument_name, code)
        if self.item is None:
            if self.exit_code == 0:
                self.exit_code = 1
            self._skip(self._rest(code), "инструмент не определён")
            return False
        self.instruments.remember(self.item)
        open_now, self.last, detail = self.market_state(self.item)
        if not open_now:
            self.exit_code = 1
            self._set(code, NO, f"{self.item.name}: торги не идут — {detail}")
            self._skip(self._rest(code), "торги не идут")
            return False
        self.step = self.step_of(self.item)
        head = f"{self.item.chosen + '; ' if self.item.chosen else ''}{self.item.name}, площадка {self.item.mic}"
        self.prepared = f"{head}; шаг {num(self.step)}, лот {num(self.quantity_of(self.item))}; {detail}"
        return True

    def open_streams(self) -> bool:
        self.streams.open()
        self.timing.sleep(self.timing.stream_ready_secs)
        failed = [name for name, handle in self.streams.handles.items() if handle.error is not None]
        state = self.streams.state()
        if failed:
            self._set(self.PREPARE, NO, f"{self.prepared}; {state}")
        else:
            self._set(self.PREPARE, YES, f"{self.prepared}; {state}")
        return True  # без стримов заявки всё равно проверяются; события получат «нет»

    def place(self, **kwargs):
        self.placed = True
        state = self.desk.place(self.item.symbol, **kwargs)
        self.guard_fill(state)
        return state

    def guard_fill(self, state) -> None:
        if state.status in FILLED_STATUSES or executed(state) > 0:
            raise ProbeHalt(f"заявка разведки исполнилась: {describe(state, self.instruments)}. На счёте "
                            "позиция — разведка её не закрывает, проверьте `account` и решите сами.")

    def cleanup(self) -> None:
        cancelled, failed = self.desk.cancel_probe_orders(keys=self.keys)
        left = [state for state in self.desk.orders()
                if is_active(state) and state.order.client_order_id in self.keys]
        detail = f"снято при уборке {len(cancelled)}" + (f", не снято {len(failed)}" if failed else "")
        if left:
            detail += "; активны: " + "; ".join(describe(state, self.instruments) for state in left)
        self._set("Ф0.99", NO if left or failed else YES, detail)
        self.streams.close()

    @property
    def keys(self) -> set:
        return self._keys

    def new_key(self) -> str:
        key = new_key(self.timing.clock)
        self._keys.add(key)
        return key

    def _run(self) -> None:
        if self._step(self.PREPARE, self.prepare):
            self.open_streams()
            self._orders()

    def _orders(self) -> None:
        raise NotImplementedError


class LimitProbe(OrderRun):
    TITLE = "Разведка Финама: лимитная заявка"
    CHECKS = (
        ("Ф0.10", "Инструмент, торги, стримы"),
        ("Ф0.11", "Ошибочная заявка с ключом"),
        ("Ф0.12", "Тот же ключ после ошибки"),
        ("Ф0.13", "Поиск по ключу"),
        ("Ф0.14", "Повтор ключа после приёма"),
        ("Ф0.15", "События стримов"),
        ("Ф0.16", "Снятие"),
        ("Ф0.17", "Снятая заявка видна"),
        ("Ф0.99", "Уборка"),
    )

    def __init__(self, *args, offset_pct: Decimal = Decimal(3), **kwargs):
        super().__init__(*args, **kwargs)
        self.offset_pct = Decimal(offset_pct)
        self.key = ""
        self.order_id = ""
        self.state = None
        self.extra_ids: list[str] = []
        self.limit_price = Decimal(0)

    def price(self, offset_pct: Decimal) -> Decimal:
        return floor_to_step(self.last * (1 - offset_pct / 100), self.step)

    def _limit(self, key: str, price: Decimal):
        self.limit_price = price
        return self.place(side=side_pb2.SIDE_BUY, kind=orders_pb2.ORDER_TYPE_LIMIT,
                          quantity=self.quantity_of(self.item), key=key, limit_price=price,
                          time_in_force=orders_pb2.TIME_IN_FORCE_DAY)

    def _orders(self) -> None:
        self.key = self.new_key()
        self._step("Ф0.11", self._bad_order)
        if not self._step("Ф0.12", self._good_order):
            self._skip(self._rest("Ф0.12")[:-1], "лимитной заявки нет")
            return
        self._step("Ф0.13", self._find)
        self._step("Ф0.14", self._repeat)
        self._step("Ф0.15", self._events)
        if self._step("Ф0.16", self._cancel):
            self._step("Ф0.17", self._after_cancel)
        else:
            self._skip(["Ф0.17"], "заявка не снята")

    # --- Ф0.11–Ф0.12 -----------------------------------------------------
    def _bad_order(self) -> bool:
        price = self.price(self.offset_pct) - self.step / 2
        try:
            state = self._limit(self.key, price)
        except BrokerCallError as exc:
            if exc.code in _TRANSIENT:
                raise
            self._set("Ф0.11", YES, f"цена {num(price)} не по шагу отклонена: {exc}")
            self.bad_rejected = True
            return True
        self.bad_rejected = False
        self.extra_ids.append(state.order_id)
        self._cancel_quietly(state.order_id)
        self._set("Ф0.11", NO, f"цена {num(price)} не по шагу принята: {describe(state, self.instruments)}; "
                               "заявка снята сразу")
        return False

    def _good_order(self) -> bool:
        price = self.price(self.offset_pct)
        if not getattr(self, "bad_rejected", False):
            self.key = self.new_key()
            state = self._accept(self.key, price, "Ф0.12", prefix="ошибки с ключом не было; ")
            return state is not None
        try:
            state = self._limit(self.key, price)
        except BrokerCallError as first:
            if first.code in _TRANSIENT:
                raise
            if no_coverage(first):
                self._set("Ф0.12", ERROR, f"недостаточно обеспечения — пополните счёт: {first}")
                return False
            # Отказ с K1: из-за ключа или из-за цены? Тот же запрос с новым ключом отвечает.
            second_key = self.new_key()
            try:
                state = self._limit(second_key, price)
            except BrokerCallError as second:
                if second.code in _TRANSIENT:
                    raise
                half = self.price(self.offset_pct / 2)
                note = f"цена {num(price)} отклонена и с новым ключом: {second}; "
                self.notes.append(f"Ф0.12: цена {num(price)} (−{num(self.offset_pct)}%) отклонена: {second}")
                self.key = self.new_key()
                state = self._accept(self.key, half, "Ф0.12", prefix=note, outcome=SKIPPED)
                return state is not None
            self.key = second_key
            self._remember(state)
            self._set("Ф0.12", NO, f"повтор ключа после ошибки отклонён: {first}; с новым ключом принята: "
                                   f"{describe(state, self.instruments)}")
            return True
        self._remember(state)
        self._set("Ф0.12", YES, f"тот же ключ после ошибки принят: {describe(state, self.instruments)}")
        return True

    def _accept(self, key: str, price: Decimal, code: str, *, prefix: str = "", outcome: str = YES):
        try:
            state = self._limit(key, price)
        except BrokerCallError as exc:
            if no_coverage(exc):
                prefix += "недостаточно обеспечения — пополните счёт; "
            self._set(code, ERROR, f"{prefix}заявку поставить не удалось: {exc}")
            return None
        self._remember(state)
        self._set(code, outcome, f"{prefix}принята: {describe(state, self.instruments)}")
        return state

    def _remember(self, state) -> None:
        self.state = state
        self.order_id = state.order_id

    def _cancel_quietly(self, order_id: str) -> None:
        try:
            self.desk.cancel(order_id)
        except BrokerCallError as exc:
            self.notes.append(f"снять заявку {order_id} не удалось: {exc}")

    # --- Ф0.13–Ф0.17 -----------------------------------------------------
    def _find(self) -> bool:
        found = self.desk.by_key(self.key)
        for state in found:
            self.guard_fill(state)
        by_id = self.desk.get(self.order_id)
        self.guard_fill(by_id)
        same = [state for state in found if state.order_id == self.order_id]
        detail = (f"в GetOrders по ключу {len(found)}: " + ("; ".join(describe(s, self.instruments) for s in found)
                                                            or "нет")
                  + f"; GetOrder по номеру: {describe(by_id, self.instruments)}")
        echo = by_id.order
        sent = self.quantity_of(self.item)
        detail += f"; отправлено количество {num(sent)} по цене {num(self.limit_price)}"
        detail += f", эхо количества {num(dec(echo.quantity))}, эхо цены {num(dec(echo.limit_price))}"
        self._set("Ф0.13", YES if same and by_id.order_id == self.order_id else NO, detail)
        return bool(same)

    def _repeat(self) -> bool:
        before = [state for state in self.desk.by_key(self.key) if is_active(state)]
        try:
            state = self._limit(self.key, self.limit_price)
        except BrokerCallError as exc:
            self._set("Ф0.14", YES, f"повтор отклонён: {exc}")
            return True
        if state.order_id == self.order_id:
            self._set("Ф0.14", YES, f"повтор вернул ту же заявку: {describe(state, self.instruments)}")
            return True
        active = [item for item in self.desk.by_key(self.key) if is_active(item)]
        self.extra_ids.append(state.order_id)
        self._cancel_quietly(state.order_id)
        self._set("Ф0.14", NO, f"повтор создал вторую заявку: первая {self.order_id}, вторая "
                               f"{describe(state, self.instruments)}; активных с ключом до {len(before)}, после "
                               f"{len(active)}; вторая снята сразу")
        return False

    def _events(self) -> bool:
        ids = {self.order_id, *self.extra_ids}

        def ours(state) -> bool:
            return state.order.client_order_id == self.key or state.order_id in ids

        arrived = self.streams.wait_order_event(ours, self.timing.event_wait_secs, self.timing.sleep)
        detail = events_summary(self.streams.order_events(ours), self.order_id) + "; " + self.streams.state()
        self._set("Ф0.15", YES if arrived else NO, detail)
        return arrived

    def _cancel(self) -> bool:
        response = self.desk.cancel(self.order_id)
        state = self.desk.get(self.order_id)

        def cancelled(item) -> bool:
            return item.order_id == self.order_id and item.status == orders_pb2.ORDER_STATUS_CANCELED

        arrived = self.streams.wait_order_event(cancelled, self.timing.event_wait_secs, self.timing.sleep)
        ok = state.status == orders_pb2.ORDER_STATUS_CANCELED
        detail = (f"ответ CancelOrder: статус {status_name(response.status)}; GetOrder: "
                  f"{describe(state, self.instruments)}; событие отмены в стриме "
                  f"{'пришло' if arrived else 'не пришло'}")
        self._set("Ф0.16", YES if ok and arrived else NO, detail)
        return ok

    def _after_cancel(self) -> bool:
        found = self.desk.by_key(self.key)
        if not found:
            self._set("Ф0.17", NO, "снятой заявки в GetOrders нет")
            return False
        self._set("Ф0.17", YES, "; ".join(describe(state, self.instruments) for state in found))
        return True
