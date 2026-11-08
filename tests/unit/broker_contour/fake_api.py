"""Фейковый API Т-Инвест для ``TInvestBroker``: заявки по ключу, снятие, сделки биржи — без сети."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.broker_contour.api import BrokerTrade, OrderFailure, OrderReport, StopReport
from src.portfolio import ContractMeta

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
D = Decimal
NG = "uid-ng-12-26"
NG_META = ContractMeta(NG, 0.001, 8.08664, 6000, 6100, expiration_date=datetime(2026, 12, 20))


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class FakeApi:
    def __init__(self):
        self.orders: dict[str, OrderReport] = {}
        self.calls: list[tuple] = []
        self.failures: list[OrderFailure | str] = []   # «accepted-lost» — заявка у брокера есть, ответ потерян
        self.prices: dict[str, Decimal] = {NG: Decimal("2.850")}
        self.band: tuple[Decimal, Decimal] | None = (Decimal("2.5"), Decimal("3.5"))
        self.margin = {"go_buy": Decimal("6228.44"), "go_sell": Decimal("6356.57"),
                       "price_step": Decimal("0.001"), "step_rub": Decimal("8.08664")}
        self.stops: dict[str, StopReport] = {}             # номер стоп-заявки → состояние у брокера
        self.stop_keys: dict[str, str] = {}                # ключ → номер стоп-заявки
        self.stop_failures: list[OrderFailure | str] = []  # «accepted-lost» — принята, ответ потерян
        self.cancel_stop_failures: list[OrderFailure] = []
        self.stop_price_rub = False                        # GetStopOrders отдаёт стоп-цену в рублях за контракт
        self.operation_items: list = []                    # OperationReport счёта (add-account-vs-emulation-report)
        self.operations_failure: OrderFailure | None = None
        self._seq = 0
        self._trade = 0
        self._stop = 0

    def _post(self, kind, uid, side, lots, price, key) -> OrderReport:
        self.calls.append(("post", kind, key, side, lots, price))
        failure = self.failures.pop(0) if self.failures else None
        if key in self.orders:
            raise OrderFailure("duplicate", "INVALID_ARGUMENT", "30057")
        if isinstance(failure, OrderFailure):
            raise failure
        self._seq += 1
        report = OrderReport(f"broker-order-{self._seq:04d}", key, "new", lots, 0, uid)
        self.orders[key] = report
        if failure == "accepted-lost":
            raise OrderFailure("unknown", "DEADLINE_EXCEEDED")
        return report

    def post_limit(self, uid, side, lots, price, key):
        return self._post("limit", uid, side, lots, price, key)

    def post_market(self, uid, side, lots, key):
        return self._post("market", uid, side, lots, None, key)

    def cancel(self, broker_order_id):
        self.calls.append(("cancel", broker_order_id))

    def order_by_key(self, key):
        self.calls.append(("state", key))
        return self.orders.get(key)

    def order_by_exchange_id(self, order_id):
        self.calls.append(("exchange-state", order_id))
        return next((report for report in self.orders.values() if report.broker_order_id == order_id), None)

    # --- стоп-заявки ----------------------------------------------------------
    def post_stop(self, uid, side, lots, stop_price, key):
        self.calls.append(("post_stop", key, side, lots, stop_price))
        failure = self.stop_failures.pop(0) if self.stop_failures else None
        if key in self.stop_keys:
            raise OrderFailure("duplicate", "INVALID_ARGUMENT", "30057")
        if isinstance(failure, OrderFailure):
            raise failure
        self._stop += 1
        stop_id = f"stop-order-{self._stop:04d}"
        shown = stop_price / D("0.001") * D("8.08664") if self.stop_price_rub else stop_price
        self.stops[stop_id] = StopReport(stop_id, uid, side, lots, shown, "active", "", self.now_for_stops)
        self.stop_keys[key] = stop_id
        if failure == "accepted-lost":
            raise OrderFailure("unknown", "DEADLINE_EXCEEDED")
        return stop_id

    now_for_stops = NOW

    def cancel_stop(self, stop_order_id):
        self.calls.append(("cancel_stop", stop_order_id))
        if self.cancel_stop_failures:
            raise self.cancel_stop_failures.pop(0)
        report = self.stops[stop_order_id]
        if report.status != "active":
            raise OrderFailure("not_found", "NOT_FOUND", "50006")
        self.stops[stop_order_id] = replace(report, status="cancelled")

    def stop_orders(self, since, until):
        self.calls.append(("stop_orders",))
        return list(self.stops.values())

    def active_stops(self) -> list[StopReport]:
        return [report for report in self.stops.values() if report.status == "active"]

    def trigger(self, stop_order_id: str, price, at: datetime = NOW) -> OrderReport:
        """Стоп-заявка сработала: брокер выставил рыночную заявку без нашего ключа и исполнил её."""
        report = self.stops[stop_order_id]
        self._seq += 1
        order_id = f"broker-order-{self._seq:04d}"
        self.stops[stop_order_id] = replace(report, status="executed", exchange_order_id=order_id)
        self._trade += 1
        trade = BrokerTrade(f"exch-{self._trade:04d}", D(str(price)), report.lots, at)
        filled = OrderReport(order_id, None, "fill", report.lots, report.lots, report.instrument_uid, (trade,))
        self.orders[f"stop-fill:{order_id}"] = filled
        return filled

    def operations(self, since, until, max_pages=10):
        self.calls.append(("operations", since, until, max_pages))
        if self.operations_failure is not None:
            raise self.operations_failure
        return [item for item in self.operation_items if item.at is None or since <= item.at <= until]

    def last_price(self, uid):
        return self.prices.get(uid)

    def price_band(self, uid):
        return self.band

    def instrument_uid(self, ticker, future):
        self.calls.append(("uid", ticker))
        return ticker

    def futures_margin(self, uid):
        self.calls.append(("margin", uid))
        return dict(self.margin)

    # --- сценарии брокера ---------------------------------------------------
    def key_of(self, broker_order_id: str) -> str:
        return next(key for key, report in self.orders.items() if report.broker_order_id == broker_order_id)

    def fill(self, key: str, lots: int, price, at: datetime = NOW) -> OrderReport:
        """Сделка биржи по заявке; отчёт как в стриме — со всеми сделками заявки."""
        report = self.orders[key]
        self._trade += 1
        trade = BrokerTrade(f"exch-{self._trade:04d}", Decimal(str(price)), lots, at)
        executed = report.lots_executed + lots
        status = "fill" if executed >= report.lots_requested else "partial"
        report = replace(report, status=status, lots_executed=executed, trades=(*report.trades, trade))
        self.orders[key] = report
        return report

    def cancelled(self, key: str) -> OrderReport:
        report = replace(self.orders[key], status="cancelled")
        self.orders[key] = report
        return report

    def posts(self, kind: str | None = None) -> list[tuple]:
        return [call for call in self.calls if call[0] == "post" and (kind is None or call[1] == kind)]
