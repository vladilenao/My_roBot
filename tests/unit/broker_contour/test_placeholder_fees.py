"""Комиссия исполнения без сделок биржи (fix-trade-close-accuracy).

Песочница 10.11: цели GOLD исполнились в книге заявок без сделок в стриме и
``stages``, брокер контура провёл их заглушками ``tinvest-order:``, и комиссия
выходов навсегда осталась оценкой 1,5 ₽ за контракт.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from t_tech.invest import schemas as s

from src.broker_contour.port import OrderFailure
from src.broker_contour.tinvest.api import report_from_state
from src.broker_contour.broker import fee_share
from src.broker_contour.orders import LedgerExecution, OrderLedger, order_key
from tests.unit.broker_contour.fake_api import NOW, FakeApi
from tests.unit.broker_contour.test_broker_fees import _Journal, _fees, _open, _sync
from tests.unit.broker_contour.test_tinvest_broker import RUB_PER_POINT, Harness

D = Decimal


def _money(value: str) -> s.MoneyValue:
    amount = D(value)
    units = int(amount)
    return s.MoneyValue(currency="rub", units=units, nano=int((amount - units) * 1_000_000_000))


def test_order_state_commission_is_read():
    state = SimpleNamespace(order_id="broker-1", order_request_id="key-1",
                            execution_report_status=s.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
                            lots_requested=5, lots_executed=5, instrument_uid="uid", stages=[],
                            executed_order_price=_money("1702148.93"), initial_order_price=_money("1702148.93"),
                            order_date=None, executed_commission=_money("851.12"))
    assert report_from_state(state).executed_fee == D("851.12")
    assert report_from_state(replace_state(state, executed_commission=_money("0"))).executed_fee is None
    assert report_from_state(replace_state(state, executed_commission=None)).executed_fee is None


def replace_state(state, **changes):
    return SimpleNamespace(**{**vars(state), **changes})


def test_fee_share_rounds_to_kopecks():
    assert fee_share(D("851.12"), 5, 5) == D("851.12")
    assert fee_share(D("100.00"), 1, 3) == D("33.33")
    assert fee_share(None, 5, 5) is None and fee_share(D("0"), 5, 5) is None


def _filled_by_sum(h, key, lots, price, fee):
    report = replace(h.api.orders[key], status="fill", lots_executed=lots, trades=(),
                     executed_rub=D(price) * lots * RUB_PER_POINT, initial_rub=D("2.845") * lots * RUB_PER_POINT,
                     executed_fee=None if fee is None else D(fee))
    h.api.orders[key] = report
    return report


def test_placeholder_carries_broker_fee(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open(lots=10)
    key = order_key(action.command_id)
    _filled_by_sum(h, key, 10, "2.846", "230.10")
    h.broker.service(h.clock())
    events = h.drain()
    assert [(e.execution_id.split(":")[0], e.fee) for e in events] == [("tinvest-order", D("230.10"))]
    assert events[0].fee_source == "broker"
    # Рестарт: журнал заявок отдаёт ту же комиссию.
    again = OrderLedger(tmp_path / "broker.sqlite3")
    assert [e.fee for e in again.executions(key)] == [D("230.10")]


def test_placeholder_without_fee_keeps_estimate(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open(lots=10)
    key = order_key(action.command_id)
    _filled_by_sum(h, key, 10, "2.846", None)
    h.broker.service(h.clock())
    assert [e.fee for e in h.drain()] == [None]                  # оценку поставит менеджер сделок


def test_placeholder_part_of_known_trades_gets_its_share(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open(lots=3)
    key = order_key(action.command_id)
    h.broker.ingest(h.api.fill(key, 1, "2.844"))                 # одна сделка биржи пришла
    h.drain()
    _filled_by_sum(h, key, 3, "2.845", "30.00")
    h.broker.resync(h.clock())
    events = h.drain()
    assert [(e.filled_quantity, e.fee) for e in events] == [(2, D("20.00"))]


def test_trimmed_placeholder_trims_fee(tmp_path):
    ledger = OrderLedger(tmp_path / "broker.sqlite3")
    ledger.add_execution(LedgerExecution("tinvest-order:broker-1:10", "key-1", 10, D("2.846"), NOW, fee=D("230.10")))
    ledger.supersede("tinvest-order:broker-1:10", lots=4)
    assert [(e.lots, e.fee) for e in ledger.executions("key-1")] == [(4, D("92.04"))]


# --- сверщик: заглушки, записанные с оценкой ------------------------------------
def _order(api, order_id, fee):
    api.orders[f"key-{order_id}"] = SimpleNamespace(broker_order_id=order_id, executed_fee=fee)


def test_sync_adjusts_placeholder_from_order_state(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest-order:broker-1:2", 2),))
    api = FakeApi()
    _order(api, "broker-1", D("849.00"))
    sync = _sync(journal, api)
    events = sync.run(NOW)
    assert [(e.adjustment_id, e.new_fee) for e in events] == [
        ("orderfee:broker-1:tinvest-order:broker-1:2", D("849.00"))]
    assert not [call for call in api.calls if call[0] == "operations"]   # сделок биржи нет — операции не нужны
    for event in events:
        journal.manager.consume(event)
    assert _fees(journal)["tinvest-order:broker-1:2"] == ("849.00", "broker")
    api.calls.clear()
    assert sync.run(NOW + timedelta(minutes=1)) == [] and api.calls == []


def test_sync_waits_for_order_fee_and_asks_rarely(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest-order:broker-1:2", 2),))
    api = FakeApi()
    _order(api, "broker-1", None)                                      # executed_commission 0
    sync = _sync(journal, api)
    assert sync.run(NOW) == [] and sync.run(NOW + timedelta(minutes=10)) == []
    assert [call for call in api.calls if call[0] == "exchange-state"] == [("exchange-state", "broker-1")]
    _order(api, "broker-1", D("849.00"))
    assert [e.new_fee for e in sync.run(NOW + timedelta(minutes=15))] == [D("849.00")]


def test_sync_order_state_failure_logs_type_only(tmp_path, caplog):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest-order:broker-1:2", 2),))

    class Failing(FakeApi):
        def order_by_exchange_id(self, order_id):
            raise OrderFailure("unknown", "UNAVAILABLE", "", "fake-secret-in-message")

    with caplog.at_level(logging.WARNING):
        assert _sync(journal, Failing()).run(NOW) == []
    assert "UNAVAILABLE" in caplog.text and "fake-secret-in-message" not in caplog.text
    assert _fees(journal)["tinvest-order:broker-1:2"] == ("12.00", "configured")
