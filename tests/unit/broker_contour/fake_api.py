"""Фейковый API Т-Инвест для ``TInvestBroker``: заявки по ключу, снятие, сделки биржи — без сети."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.broker_contour.api import BrokerTrade, OrderFailure, OrderReport
from src.portfolio import ContractMeta

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
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
        self._seq = 0
        self._trade = 0

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
