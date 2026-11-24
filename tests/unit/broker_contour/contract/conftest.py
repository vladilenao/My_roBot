"""Площадки контрактного набора порта: адаптер брокера поверх своего фейка (refactor-broker-port, решение 8).

Площадка отдаёт порт, книгу цен и данные брокера для ``LiveBroker`` и управляет фейком брокера: исполнить
заявку, снять, сработать стоп-заявку, потерять ответ. Сценарии набора знают только этот интерфейс.
На Ф3 рядом встанет площадка Финама — тот же набор пройдёт и на ней.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.broker_contour.prices import PriceBook
from src.broker_contour.tinvest.fees import FILLS_PREFIX
from src.broker_contour.tinvest.port import TInvestPort
from src.broker_contour.tinvest.session import MARGIN_EVERY
from tests.unit.broker_contour.fake_api import FakeApi


class TInvestVenue:
    """Адаптер Т-Инвест поверх фейкового ``TInvestApi``."""

    name = "tinvest"
    fills_prefix = FILLS_PREFIX
    margin_every = MARGIN_EVERY

    def __init__(self):
        self.api = FakeApi()
        self.book = PriceBook()
        self.port = TInvestPort(self.api, self.book)

    # --- что видит брокер ---------------------------------------------------------
    def posts(self, kind: str | None = None) -> list[tuple]:
        return self.api.posts(kind)

    def stop_posts(self) -> int:
        return sum(1 for call in self.api.calls if call[0] == "post_stop")

    def active_stops(self) -> list[tuple[str, int, Decimal]]:
        return [(report.side, report.lots, report.stop_price) for report in self.api.active_stops()]

    def cancelled_orders(self) -> set[str]:
        return {call[1] for call in self.api.calls if call[0] == "cancel"}

    def broker_order_id(self, key: str) -> str:
        return self.api.orders[key].broker_order_id

    # --- события брокера -----------------------------------------------------------
    def fill(self, key: str, lots: int, price: str):
        """Сделка биржи по заявке — отчёт стрима, как его отдаёт порт."""
        return self.port.order_report(self.api.fill(key, lots, price))

    def cancelled(self, key: str):
        return self.port.order_report(self.api.cancelled(key))

    def trigger(self, price: str):
        """Единственная активная стоп-заявка сработала; отчёт стрима по порождённой заявке."""
        stop_id = self.api.active_stops()[0].stop_order_id
        return stop_id, self.port.order_report(self.api.trigger(stop_id, price))

    def lose_next_order_answer(self) -> None:
        self.api.failures.append("accepted-lost")

    def lose_next_stop_answer(self) -> None:
        self.api.stop_failures.append("accepted-lost")


@pytest.fixture(params=[TInvestVenue], ids=lambda venue: venue.name)
def venue(request):
    return request.param()
