"""``probe-stops`` и ``probe-trigger``: стоп-заявка далеко от рынка и срабатывание стопа (add-finam-probe, решение 5).

``probe-stops`` ставит ``ORDER_TYPE_STOP`` на 1 лот в ``--offset-pct`` процентах
от рынка со сроком ``GOOD_TILL_CANCEL`` и смотрит, как брокер её показывает;
с ``--keep`` оставляет её на ночь. ``probe-trigger`` — единственное действие
со сделкой: стоп у рынка, ожидание срабатывания, порождённая заявка, сделка,
позиция, закрытие своего объёма и комиссия.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from src.finam.checks import NO, SKIPPED, YES, ceil_to_step, floor_to_step, interval, num, ts
from src.finam.instruments import dec
from src.finam.orders import (
    FINAL_STATUSES,
    SIDE_NAMES,
    describe,
    events_summary,
    executed,
    is_active,
    status_name,
)
from src.finam.probe import OrderRun
from src.finam.proto.grpc.tradeapi.v1 import side_pb2
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.sandbox.gateway import BrokerCallError

SIDES = {"buy": side_pb2.SIDE_BUY, "sell": side_pb2.SIDE_SELL}
OPPOSITE = {side_pb2.SIDE_BUY: side_pb2.SIDE_SELL, side_pb2.SIDE_SELL: side_pb2.SIDE_BUY}
KEEP_NOTE = ("Стоп-заявка оставлена по --keep. Проверьте её `python -m src.finam account{demo}` утром и после "
             "выходных, затем снимите `python -m src.finam cleanup{demo}`.")


class StopRunBase(OrderRun):
    """Стоп-заявка на 1 лот: покупка ``LAST_UP`` выше рынка или продажа ``LAST_DOWN`` ниже."""

    def __init__(self, *args, side: str = "buy", **kwargs):
        super().__init__(*args, **kwargs)
        self.side = SIDES[side]
        self.stop_key = ""
        self.stop_id = ""
        self.stop_price = Decimal(0)

    @property
    def condition(self) -> int:
        return orders_pb2.STOP_CONDITION_LAST_UP if self.side == side_pb2.SIDE_BUY else orders_pb2.STOP_CONDITION_LAST_DOWN

    def place_stop(self, key: str, price: Decimal):
        return self.place(side=self.side, kind=orders_pb2.ORDER_TYPE_STOP, quantity=self.quantity_of(self.item),
                          key=key, stop_price=price, stop_condition=self.condition,
                          valid_before=orders_pb2.VALID_BEFORE_GOOD_TILL_CANCEL)

    def guard_fill(self, state) -> None:
        # У стоп-заявки «исполнена» значит «сработала»: это проверяет probe-trigger, а не сбой разведки.
        if state.order.type in (orders_pb2.ORDER_TYPE_STOP, orders_pb2.ORDER_TYPE_STOP_LIMIT):
            return
        super().guard_fill(state)

    def stop_events(self) -> str:
        def ours(state) -> bool:
            return state.order.client_order_id == self.stop_key or state.order_id == self.stop_id

        self.streams.wait_order_event(ours, self.timing.event_wait_secs, self.timing.sleep)
        return events_summary(self.streams.order_events(ours), self.stop_id) + "; " + self.streams.state()


class StopProbe(StopRunBase):
    TITLE = "Разведка Финама: стоп-заявка"
    CHECKS = (
        ("Ф0.10", "Инструмент, торги, стримы"),
        ("Ф0.20", "Стоп-заявка принята"),
        ("Ф0.21", "Видна в GetOrders"),
        ("Ф0.22", "GetOrder по номеру"),
        ("Ф0.23", "Повтор ключа стоп-заявки"),
        ("Ф0.24", "События стримов"),
        ("Ф0.25", "Снятие"),
        ("Ф0.99", "Уборка"),
    )

    def __init__(self, *args, offset_pct: Decimal = Decimal(10), keep: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset_pct = Decimal(offset_pct)
        self.keep = keep

    def _orders(self) -> None:
        if not self._step("Ф0.20", self._place):
            self._skip(self._rest("Ф0.20")[:-1], "стоп-заявки нет")
            return
        self._step("Ф0.21", self._visible)
        self._step("Ф0.22", self._by_id)
        self._step("Ф0.23", self._repeat)
        self._step("Ф0.24", lambda: self._set("Ф0.24", YES, self.stop_events()) or True)
        self._step("Ф0.25", self._cancel)

    def _place(self) -> bool:
        if self.side == side_pb2.SIDE_BUY:
            self.stop_price = ceil_to_step(self.last * (1 + self.offset_pct / 100), self.step)
        else:
            self.stop_price = floor_to_step(self.last * (1 - self.offset_pct / 100), self.step)
        self.stop_key = self.new_key()
        try:
            state = self.place_stop(self.stop_key, self.stop_price)
        except BrokerCallError as exc:
            self._set("Ф0.20", NO, f"стоп {SIDE_NAMES[self.side]} по {num(self.stop_price)} при последней "
                                   f"{num(self.last)} отклонён: {exc}")
            return False
        self.stop_id = state.order_id
        self._set("Ф0.20", YES, f"стоп {SIDE_NAMES[self.side]} по {num(self.stop_price)} при последней "
                                f"{num(self.last)}: {describe(state, self.instruments)}")
        return True

    def _visible(self) -> bool:
        found = self.desk.by_key(self.stop_key)
        if not found:
            self._set("Ф0.21", NO, "стоп-заявки с нашим ключом в GetOrders нет")
            return False
        state = found[0]
        echo = state.order
        detail = (f"{describe(state, self.instruments)}; отправлена стоп-цена {num(self.stop_price)}, эхо "
                  f"{num(dec(echo.stop_price))}; initial_quantity "
                  f"{'заполнен' if state.HasField('initial_quantity') else 'пуст'}; triggered_order_id "
                  f"{state.triggered_order_id or 'пуст'}; time_in_force "
                  f"{orders_pb2.TimeInForce.Name(echo.time_in_force)}")
        self._set("Ф0.21", YES, detail)
        return True

    def _by_id(self) -> bool:
        state = self.desk.get(self.stop_id)
        self._set("Ф0.22", YES if state.order_id == self.stop_id else NO, describe(state, self.instruments))
        return True

    def _repeat(self) -> bool:
        try:
            state = self.place_stop(self.stop_key, self.stop_price)
        except BrokerCallError as exc:
            self._set("Ф0.23", YES, f"повтор отклонён: {exc}")
            return True
        if state.order_id == self.stop_id:
            self._set("Ф0.23", YES, f"повтор вернул ту же стоп-заявку: {describe(state, self.instruments)}")
            return True
        try:
            self.desk.cancel(state.order_id)
            note = "вторая снята сразу"
        except BrokerCallError as exc:
            note = f"вторую снять не удалось: {exc}"
        self._set("Ф0.23", NO, f"повтор создал вторую стоп-заявку: {describe(state, self.instruments)}; {note}")
        return False

    def _cancel(self) -> bool:
        if self.keep:
            self._set("Ф0.25", SKIPPED, "оставлена по --keep")
            self.notes.append(KEEP_NOTE.format(demo=" --demo" if self.demo else ""))
            return True
        response = self.desk.cancel(self.stop_id)
        found = self.desk.by_key(self.stop_key)
        cancelled = [state for state in found if state.order_id == self.stop_id and not is_active(state)]
        detail = (f"ответ CancelOrder: статус {status_name(response.status)}; после снятия в GetOrders: "
                  + ("; ".join(describe(state, self.instruments) for state in found) or "нет"))
        self._set("Ф0.25", YES if cancelled else NO, detail)
        return bool(cancelled)

    def cleanup(self) -> None:
        if not self.keep:
            super().cleanup()
            return
        # С --keep снимаются только лишние заявки разведки, сама стоп-заявка остаётся.
        for state in self.desk.orders():
            order = state.order
            if (is_active(state) and order.client_order_id.startswith("fp") and state.order_id != self.stop_id
                    and order.symbol == self.item.symbol):
                try:
                    self.desk.cancel(state.order_id)
                except BrokerCallError as exc:
                    self.notes.append(f"снять {state.order_id} не удалось: {exc}")
        kept = self.desk.get(self.stop_id) if self.stop_id else None
        self._set("Ф0.99", YES, "стоп-заявка оставлена: " + (describe(kept, self.instruments) if kept else "нет"))
        self.streams.close()


class TriggerProbe(StopRunBase):
    TITLE = "Разведка Финама: срабатывание стопа"
    CHECKS = (
        ("Ф0.10", "Инструмент, торги, стримы"),
        ("Ф0.30", "Стоп у рынка принят"),
        ("Ф0.31", "Срабатывание"),
        ("Ф0.32", "Порождённая заявка"),
        ("Ф0.33", "Сделка и единицы"),
        ("Ф0.34", "Позиция"),
        ("Ф0.35", "Закрытие"),
        ("Ф0.36", "Комиссия в транзакциях"),
        ("Ф0.99", "Уборка"),
    )

    def __init__(self, *args, ticks: int = 1, wait_min: float = 15, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticks = ticks
        self.wait_min = wait_min
        self.triggered_id = ""
        self.before_qty = Decimal(0)
        self.before_account = None
        self.trade_times: list = []
        self.close_id = ""

    # --- позиция ---------------------------------------------------------
    def account(self):
        return self.gateway.call("accounts.GetAccount", accounts_pb2.GetAccountRequest(account_id=self.account_id))

    def position_qty(self, account=None) -> Decimal:
        account = account or self.account()
        for position in account.positions:
            if position.symbol == self.item.symbol:
                return dec(position.quantity) or Decimal(0)
        return Decimal(0)

    # --- ход -------------------------------------------------------------
    def _orders(self) -> None:
        self.before_account = self.account()
        self.before_qty = self.position_qty(self.before_account)
        if not self._step("Ф0.30", self._place):
            self._skip(self._rest("Ф0.30")[:-1], "стоп-заявки нет")
            return
        if not self._step("Ф0.31", self._wait_trigger):
            self._skip(self._rest("Ф0.31")[:-1], "стоп не сработал")
            return
        self._step("Ф0.32", self._triggered_order)
        self._step("Ф0.33", self._trade)
        self._step("Ф0.34", self._position)
        self._step("Ф0.35", self._close)
        self._step("Ф0.36", self._fees)

    def _place(self) -> bool:
        distance = self.step * self.ticks
        if self.side == side_pb2.SIDE_BUY:
            self.stop_price = floor_to_step(self.last, self.step) + distance
        else:
            self.stop_price = ceil_to_step(self.last, self.step) - distance
        self.stop_key = self.new_key()
        try:
            state = self.place_stop(self.stop_key, self.stop_price)
        except BrokerCallError as exc:
            self._set("Ф0.30", NO, f"стоп по {num(self.stop_price)} при последней {num(self.last)} отклонён: {exc}")
            return False
        self.stop_id = state.order_id
        self._set("Ф0.30", YES, f"стоп {SIDE_NAMES[self.side]} по {num(self.stop_price)} при последней "
                                f"{num(self.last)}, позиция до {num(self.before_qty)}: "
                                f"{describe(state, self.instruments)}")
        return True

    def _wait_trigger(self) -> bool:
        history: list[str] = []
        last_status = None
        deadline = self.timing.clock() + timedelta(minutes=self.wait_min)
        while True:
            state = self.desk.get(self.stop_id)
            if state.status != last_status:
                last_status = state.status
                history.append(f"{self.timing.clock():%H:%M:%S} {status_name(state.status)}")
            if state.triggered_order_id:
                self.triggered_id = state.triggered_order_id
                self._set("Ф0.31", YES, f"сработал: статусы {' → '.join(history)}; triggered_order_id "
                                        f"{self.triggered_id}; {describe(state, self.instruments)}")
                return True
            if state.status in FINAL_STATUSES:
                self._set("Ф0.31", NO, f"стоп завершился без triggered_order_id: статусы {' → '.join(history)}; "
                                       f"{describe(state, self.instruments)}")
                return False
            if self.timing.clock() >= deadline:
                self._set("Ф0.31", SKIPPED, f"цена не дошла за {self.wait_min:g} мин: статусы "
                                            f"{' → '.join(history)}")
                return False
            self.timing.sleep(self.timing.poll_secs)

    def _triggered_order(self) -> bool:
        deadline = self.timing.clock() + timedelta(seconds=self.timing.event_wait_secs)
        state = self.desk.get(self.triggered_id)
        while is_active(state) and self.timing.clock() < deadline:
            self.timing.sleep(self.timing.poll_secs / 5)
            state = self.desk.get(self.triggered_id)

        def ours(item) -> bool:
            return item.order_id == self.triggered_id

        events = events_summary(self.streams.order_events(ours), self.triggered_id)
        stop = self.desk.get(self.stop_id)
        self._set("Ф0.32", YES if executed(state) > 0 else NO,
                  f"порождённая: {describe(state, self.instruments)}; стоп после срабатывания: "
                  f"{describe(stop, self.instruments)}; стримы: {events}")
        return True

    def _trade(self) -> bool:
        def ours(trade) -> bool:
            return trade.order_id == self.triggered_id

        self.streams.wait_trade_event(ours, self.timing.event_wait_secs, self.timing.sleep)
        streamed = self.streams.trade_events(ours)
        parts = []
        for name, items in streamed.items():
            parts.append(f"{name}: " + ("; ".join(
                f"цена {num(dec(trade.price))}, размер {num(dec(trade.size))}, валюта {trade.currency or 'пусто'}, "
                f"комиссия {num(dec(trade.commission.value)) if trade.HasField('commission') else 'нет'}"
                for _, trade in items) or "сделок нет"))
        history = self._history_trades()
        self.trade_times = [trade.timestamp for trade in history]
        parts.append("Trades сразу: " + self._trades_text(history))
        self.timing.sleep(self.timing.minute_secs)
        parts.append("Trades через минуту: " + self._trades_text(self._history_trades()))
        if self.last and history:
            ratio = (dec(history[0].price) or Decimal(0)) / self.last
            parts.append(f"цена сделки / последняя цена Финама = {ratio:.4f}")
        self._set("Ф0.33", YES if history or any(streamed.values()) else NO, "; ".join(parts))
        return True

    def _history_trades(self) -> list:
        now = self.timing.clock()
        trades = self.gateway.call("accounts.Trades", accounts_pb2.TradesRequest(
            account_id=self.account_id, limit=100, interval=interval(self.since(5), now + timedelta(minutes=1)))).trades
        return [trade for trade in trades if trade.order_id in (self.triggered_id, self.close_id)]

    @staticmethod
    def _trades_text(trades) -> str:
        return "; ".join(
            f"{ts(trade.timestamp)} {SIDE_NAMES.get(trade.side, '?')} цена {num(dec(trade.price))} "
            f"{trade.currency or ''}, размер {num(dec(trade.size))}, комиссия "
            f"{num(dec(trade.commission.value)) + ' ' + trade.commission.currency if trade.HasField('commission') else 'нет'}"
            for trade in trades) or "сделок нет"

    def _position(self) -> bool:
        after = self.account()
        qty = self.position_qty(after)
        position = next((item for item in after.positions if item.symbol == self.item.symbol), None)
        detail = f"позиция до {num(self.before_qty)}, после {num(qty)}"
        if position is not None:
            detail += (f"; average_price {num(dec(position.average_price))}, current_price "
                       f"{num(dec(position.current_price))}, maintenance_margin {num(dec(position.maintenance_margin))}"
                       f", unrealized_pnl {num(dec(position.unrealized_pnl))}")
        detail += f"; equity {num(dec(self.before_account.equity))} → {num(dec(after.equity))}"
        portfolio = after.WhichOneof("portfolio")
        if portfolio in ("portfolio_mc", "portfolio_forts"):
            before = getattr(self.before_account, portfolio)
            now = getattr(after, portfolio)
            detail += f"; available_cash {num(dec(before.available_cash))} → {num(dec(now.available_cash))}"
        self._set("Ф0.34", YES if qty != self.before_qty else NO, detail)
        return True

    def _close(self) -> bool:
        opened = self.position_qty() - self.before_qty
        if opened == 0:
            self._set("Ф0.35", SKIPPED, "разведка позицию не открыла — закрывать нечего")
            return True
        side = side_pb2.SIDE_SELL if opened > 0 else side_pb2.SIDE_BUY
        key = self.new_key()
        state = self.place_close(side, abs(opened), key)
        self.close_id = state.order_id
        deadline = self.timing.clock() + timedelta(seconds=self.timing.event_wait_secs)
        while is_active(state) and self.timing.clock() < deadline:
            self.timing.sleep(self.timing.poll_secs / 5)
            state = self.desk.get(self.close_id)
        qty = self.position_qty()
        self._set("Ф0.35", YES if qty == self.before_qty else NO,
                  f"закрытие {SIDE_NAMES[side]} {num(abs(opened))}: {describe(state, self.instruments)}; позиция "
                  f"после {num(qty)} (до разведки {num(self.before_qty)})")
        return True

    def place_close(self, side: int, quantity: Decimal, key: str):
        self.placed = True
        return self.desk.place(self.item.symbol, side=side, kind=orders_pb2.ORDER_TYPE_MARKET, quantity=quantity,
                               key=key, time_in_force=orders_pb2.TIME_IN_FORCE_DAY)

    def _fees(self) -> bool:
        self.timing.sleep(self.timing.transactions_wait_secs)
        now = self.timing.clock()
        items = self.gateway.call("accounts.Transactions", accounts_pb2.TransactionsRequest(
            account_id=self.account_id, limit=200, interval=interval(self.since(5), now + timedelta(minutes=1)))
        ).transactions
        ours = [item for item in items if item.symbol == self.item.symbol or self.item.symbol in item.symbols]
        fees = [item for item in items
                if item.transaction_category == accounts_pb2.Transaction.TransactionCategory.COMMISSION]
        text = "; ".join(
            f"{ts(item.timestamp)} {accounts_pb2.Transaction.TransactionCategory.Name(item.transaction_category)} "
            f"«{item.transaction_name}» {num(dec(item.change))} {item.change.currency_code}, id {item.id}"
            + (f", сделка {num(dec(item.trade.size))} по {num(dec(item.trade.price))}" if item.HasField("trade")
               else "") for item in {id(x): x for x in ours + fees}.values())
        trades = "; ".join(ts(item) for item in self.trade_times) or "нет"
        self._set("Ф0.36", YES if fees else NO, f"время сделок {trades}; транзакции: {text or 'нет'}")
        return bool(fees)

    def cleanup(self) -> None:
        notes = []
        if self.stop_id:
            try:
                stop = self.desk.get(self.stop_id)
                if is_active(stop):
                    self.desk.cancel(self.stop_id)
                    notes.append("стоп снят")
            except BrokerCallError as exc:
                notes.append(f"стоп не снят: {exc}")
        cancelled, failed = self.desk.cancel_probe_orders(self.item.symbol)
        if cancelled:
            notes.append(f"снято заявок разведки {len(cancelled)}")
        try:
            opened = self.position_qty() - self.before_qty
        except BrokerCallError as exc:
            self._set("Ф0.99", NO, f"позицию проверить не удалось: {exc}; " + "; ".join(notes))
            self.streams.close()
            return
        ok = not failed
        if opened != 0:
            side = side_pb2.SIDE_SELL if opened > 0 else side_pb2.SIDE_BUY
            try:
                state = self.place_close(side, abs(opened), self.new_key())
                notes.append(f"закрыто по рынку {num(abs(opened))}: {describe(state, self.instruments)}")
            except BrokerCallError as exc:
                ok = False
                notes.append(f"позиция разведки {num(opened)} не закрыта: {exc} — закройте вручную")
        self._set("Ф0.99", YES if ok else NO, "; ".join(notes) or "убирать нечего")
        self.streams.close()


__all__ = ["KEEP_NOTE", "StopProbe", "TriggerProbe"]
