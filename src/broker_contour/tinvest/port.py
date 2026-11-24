"""Адаптер Т-Инвест к портам контура на брокере (refactor-broker-port, design.md решения 2–3).

``TInvestPort`` стоит поверх ``TInvestApi`` и отвечает на намерения контура:

- отчёты переводит в пункты: суммы Т-Инвест у фьючерса — рубли за контракт (``OrderStage`` в
  ``GetOrderState``, ``executed_order_price``); цена сделки стрима — уже пункты;
- ``order_state`` ищет заявку по нашему ключу, а без ключа — по номеру биржи;
- ``poll_stops`` читает ``GetStopOrders`` одним вызовом: стоп-заявку с номером — по номеру, с потерянным
  ответом — по инструменту, стороне, объёму, цене и времени (своего ключа в ответах Т-Инвест нет);
- ``max_lots`` (``GetMaxLots``) — только для «Счёта»: сборка передаёт адаптер контуру как ``MarginLimits``.

Логика перенесена из ``broker.py`` и ``stops.py`` без изменений.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterator

from src.broker_contour.port import BrokerTrade, OrderReport, StopMatch, StopQuery, StopReport
from src.broker_contour.prices import PriceBook, executed_lots
from src.broker_contour.tinvest.api import TInvestApi, TInvestOrderReport, TInvestStopReport
from src.broker_contour.tinvest.prices import fill_average, points_from_rub, trade_price

# Стоп-заявка с потерянным ответом ищется среди созданных не раньше отправки минус запас (fix-stop-key-duplicate).
FOUND_SLACK = timedelta(seconds=5)


class TInvestPort:
    """Порты ``OrderPort``, ``StopPort``, ``MarketPort``, ``OrderFeed`` и ``MarginLimits`` у Т-Инвест."""

    def __init__(self, api: TInvestApi, book: PriceBook):
        self._api = api
        self._book = book

    # --- перевод отчётов -------------------------------------------------------
    def order_report(self, report: TInvestOrderReport, uid: str | None = None) -> OrderReport:
        """Отчёт Т-Инвест → отчёт порта в пунктах: цены сделок и средняя исполнения без сделок."""
        card = self._book.by_uid(uid or report.instrument_uid)
        trades = tuple(BrokerTrade(t.trade_id, trade_price(card, t.price, t.in_rub), t.quantity, t.at)
                       for t in report.trades)
        average = None
        if report.executed_rub:
            lots = executed_lots(card, report.lots_executed, report.lots_requested)
            average = fill_average(card, report.executed_rub, lots, report.initial_rub, report.lots_requested)
        return OrderReport(report.broker_order_id, report.key, report.status, report.lots_requested,
                           report.lots_executed, report.instrument_uid, trades, temporary=report.temporary,
                           at=report.at, executed_fee=report.executed_fee, average=average)

    # --- OrderPort -------------------------------------------------------------
    def post_limit(self, uid: str, side: str, lots: int, price: Decimal, key: str) -> OrderReport:
        return self.order_report(self._api.post_limit(uid, side, lots, price, key), uid)

    def post_market(self, uid: str, side: str, lots: int, key: str) -> OrderReport:
        return self.order_report(self._api.post_market(uid, side, lots, key), uid)

    def cancel(self, broker_order_id: str) -> None:
        self._api.cancel(broker_order_id)

    def order_state(self, uid: str, key: str | None, number: str | None) -> OrderReport | None:
        """По нашему ключу (``GetOrderState`` по ``order_request_id``); ключа нет — по номеру биржи."""
        report = self._api.order_by_key(key) if key else self._api.order_by_exchange_id(number or "")
        return None if report is None else self.order_report(report, uid)

    # --- StopPort --------------------------------------------------------------
    def post_stop(self, uid: str, side: str, lots: int, stop_price: Decimal, key: str) -> str:
        return self._api.post_stop(uid, side, lots, stop_price, key)

    def cancel_stop(self, stop_order_id: str) -> None:
        self._api.cancel_stop(stop_order_id)

    def poll_stops(self, queries: list[StopQuery], known: set[str], since: datetime,
                   until: datetime) -> list[StopMatch]:
        """Один ``GetStopOrders`` за период; по запросу — его стоп-заявка или итог поиска.

        Запросы разбираются по порядку, найденная стоп-заявка сразу считается привязанной: одна стоп-заявка
        брокера — не больше чем одной строке журнала.
        """
        reports = self._api.stop_orders(since, until)
        by_id = {report.stop_order_id: report for report in reports}
        bound = set(known)
        result = []
        for query in queries:
            report = by_id.get(query.stop_order_id) if query.stop_order_id else None
            if report is not None and not query.lost:
                result.append(StopMatch(_stop(report)))
                continue
            if not query.search:
                result.append(StopMatch(None))
                continue
            found = self._candidates(query, reports, bound)
            if len(found) == 1:
                bound.add(found[0].stop_order_id)
                result.append(StopMatch(_stop(found[0]), candidates=1, searched=True))
            else:
                result.append(StopMatch(None, candidates=len(found), searched=True))
        return result

    def _candidates(self, query: StopQuery, reports: list[TInvestStopReport], bound: set[str]):
        """Активные непривязанные стоп-заявки брокера, похожие на строку: инструмент, сторона, объём, цена, время."""
        sent = query.sent_at - FOUND_SLACK
        card = self._book.by_uid(query.instrument_uid)
        return [report for report in reports
                if report.status == "active" and report.stop_order_id not in bound
                and report.instrument_uid == query.instrument_uid and report.side == query.side
                and report.lots == query.lots and _same_stop_price(card, report.stop_price, query.stop_price)
                and (report.created_at is None or report.created_at >= sent)]

    # --- MarketPort ------------------------------------------------------------
    def last_price(self, uid: str) -> Decimal | None:
        return self._api.last_price(uid)

    def price_band(self, uid: str) -> tuple[Decimal, Decimal] | None:
        return self._api.price_band(uid)

    def instrument_uid(self, ticker: str, future: bool) -> str:
        return self._api.instrument_uid(ticker, future)

    def futures_margin(self, uid: str) -> dict:
        return self._api.futures_margin(uid)

    # --- MarginLimits («Счёт») -------------------------------------------------
    def max_lots(self, uid: str, side: str, price: Decimal) -> int:
        return self._api.max_lots(uid, side, price)

    # --- OrderFeed -------------------------------------------------------------
    def order_stream(self) -> Iterator[OrderReport | None]:
        for report in self._api.order_stream():
            yield None if report is None else self.order_report(report)


def _stop(report: TInvestStopReport) -> StopReport:
    return StopReport(report.stop_order_id, report.instrument_uid, report.side, report.lots, report.status,
                      report.exchange_order_id, report.created_at)


def _same_stop_price(card, reported: Decimal | None, price: Decimal) -> bool:
    """Стоп-цена ``GetStopOrders`` у фьючерса бывает в рублях за контракт (разведка П19): оба прочтения."""
    if reported is None:
        return False
    return reported == price or (card.future and points_from_rub(card, reported) == price)

