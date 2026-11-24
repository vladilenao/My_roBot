"""Старые базы контура на новой версии (refactor-broker-port, tasks 2.7–2.8).

Номера исполнений «Счёта» и песочницы, записанные прежней версией, — ``tinvest:<сделка биржи>`` и
``tinvest-order:<заявка>:<лоты>``. Новая версия берёт префикс у адаптера Т-Инвест и не должна задвоить
исполнения, когда брокер после рестарта повторно присылает те же сделки, а ждущие комиссии должна находить
по прежним номерам.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

from src.broker_contour.orders import ACCEPTED, FILLED, LedgerExecution, LedgerOrder, OrderLedger, order_key
from src.broker_contour.prices import PriceBook
from src.broker_contour.tinvest.api import BrokerTrade, OrderReport
from src.broker_contour.tinvest.fees import FeeSync
from src.broker_contour.tinvest.port import TInvestPort
from tests.unit.broker_contour.fake_api import NG, NG_META, NOW, Clock, FakeApi, live_broker
from tests.unit.broker_contour.test_broker_fees import NOW as FEES_NOW, _Journal, _open
from tests.unit.broker_contour.test_tinvest_broker import RUB_PER_POINT, _plan

D = Decimal
TRADE = "trade-ng-1"


def _old_ledger(path) -> OrderLedger:
    """``broker.sqlite3`` прежней версии: вход на 2 лота исполнен сделкой ``tinvest:exch-0001``, проведён в журнал."""
    ledger = OrderLedger(path, clock=Clock())
    key = order_key(f"{TRADE}:entry")
    ledger.create(LedgerOrder(key, f"{TRADE}:entry", TRADE, "entry", NG, "BUY", "limit", 2, D("2.845"),
                              state=ACCEPTED, broker_order_id="broker-order-0001", job=f"{TRADE}:entry"))
    ledger.add_execution(LedgerExecution("tinvest:exch-0001", key, 2, D("2.846"), NOW))
    ledger.mark_consumed("tinvest:exch-0001")
    ledger.update(key, state=FILLED, lots_executed=2, average=D("2.846"), reported=1)
    return ledger


def _restart(tmp_path, api: FakeApi):
    ledger = OrderLedger(tmp_path / "broker.sqlite3", clock=Clock())
    journal = {"tinvest:exch-0001"}
    broker = live_broker(api, ledger, contour="sandbox", clock=Clock(), journal_has=journal.__contains__)
    broker.set_contracts({NG: NG_META})
    broker.register_trade(_plan(TRADE))
    return broker, ledger


def test_old_trade_executions_not_doubled_after_restart(tmp_path):
    _old_ledger(tmp_path / "broker.sqlite3").close()
    api = FakeApi()
    key = order_key(f"{TRADE}:entry")
    api.orders[key] = OrderReport("broker-order-0001", key, "fill", 2, 2, NG,
                                  (BrokerTrade("exch-0001", D("2.846"), 2, NOW),))
    broker, ledger = _restart(tmp_path, api)
    broker.recover(Clock()())
    broker.resync(Clock()())
    assert broker.drain_addressed_events() == []
    rows = sqlite3.connect(tmp_path / "broker.sqlite3").execute("SELECT execution_id FROM broker_executions").fetchall()
    assert rows == [("tinvest:exch-0001",)]
    assert ledger.get(key).lots_executed == 2


def test_old_placeholder_not_doubled_when_trades_arrive(tmp_path):
    """Заглушка ``tinvest-order:`` прежней версии и сделки той же заявки после рестарта — лоты не растут."""
    ledger = OrderLedger(tmp_path / "broker.sqlite3", clock=Clock())
    key = order_key(f"{TRADE}:entry")
    ledger.create(LedgerOrder(key, f"{TRADE}:entry", TRADE, "entry", NG, "BUY", "limit", 2, D("2.845"),
                              state=ACCEPTED, broker_order_id="broker-order-0001", job=f"{TRADE}:entry"))
    ledger.add_execution(LedgerExecution("tinvest-order:broker-order-0001:2", key, 2, D("2.846"), NOW))
    ledger.mark_consumed("tinvest-order:broker-order-0001:2")
    ledger.update(key, lots_executed=2, average=D("2.846"))      # процесс остановился до итога заявки
    ledger.close()
    api = FakeApi()
    api.orders[key] = OrderReport("broker-order-0001", key, "fill", 2, 2, NG,
                                  (BrokerTrade("exch-0007", D("2.846"), 2, NOW),))
    broker, ledger = _restart(tmp_path, api)
    broker.recover(Clock()())
    events = broker.drain_addressed_events()
    assert sum(event.filled_quantity for event in events) == 0
    assert sum(execution.lots for execution in ledger.executions(key)) == 2


def test_old_fee_numbers_found_by_fee_sync(tmp_path):
    """Ждущие комиссии журнала сделок прежней версии (``tinvest:exch-…``) находятся по номеру сделки биржи."""
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal)
    pending = FeeSync(FakeApi(), lambda: journal.storage.connection).pending(FEES_NOW)
    assert [(fill.execution_id, fill.trade_number) for fill in pending] == [
        ("tinvest:exch-1", "exch-1"), ("tinvest:exch-2", "exch-2")]


def test_placeholder_average_for_shares_in_pieces():
    """GAZP (лот 10): ``lots_executed`` в штуках (100 на 10 лотов) — средняя заглушки та же, что до переноса.

    До переноса ``_refresh`` переводил штуки в лоты по лотам строки журнала, теперь адаптер — по
    ``lots_requested`` брокера; у Т-Инвест они совпадают.
    """
    from src.portfolio import ContractMeta

    book = PriceBook()
    port = TInvestPort(FakeApi(), book)
    gazp = "uid-gazp"
    from src.broker_contour.prices import card_from_meta

    book.cards[gazp] = card_from_meta(gazp, ContractMeta(gazp, 0.01, 0.1, 0, 0))
    raw = OrderReport("broker-order-0009", "k", "fill", 10, 100, gazp, executed_rub=D("29630.00"),
                      initial_rub=D("29600.00"))
    assert port.order_report(raw).average == D("296.30")
    future = OrderReport("broker-order-0010", "k", "fill", 3, 3, NG, executed_rub=D("2.845") * 3 * RUB_PER_POINT,
                         initial_rub=D("2.845") * 3 * RUB_PER_POINT)
    book.cards[NG] = card_from_meta(NG, NG_META)
    assert port.order_report(future).average == D("2.845")
    assert port.order_report(replace(future, executed_rub=None)).average is None
