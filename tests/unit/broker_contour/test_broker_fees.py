"""Настоящие комиссии из операций брокера (add-account-vs-emulation-report, решение 6)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from t_tech.invest import schemas as s
from t_tech.invest.utils import decimal_to_quotation

from src.bot import control_views as views
from src.bot.trades_view import ContourTrades
from src.broker import ExecutionEvent, ExecutionStatus
from src.broker.port import FeeSource
from src.broker_contour.api import OperationReport, OrderFailure, TInvestApi
from src.broker_contour.fees import FeeSync, adjustments
from src.broker_contour.process import BrokerContour
from src.sandbox.gateway import BrokerCallError
from src.trade_management.actions import CloseTrade, OpenTrade
from src.trade_management.models import ProfileSnapshot, TradePlan
from tests.unit.bot.test_contour_trades_api import CATALOG, _Journal
from tests.unit.broker_contour.fake_api import FakeApi

UTC = timezone.utc
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
D = Decimal
TRADE = "future-ng-macd:NGX6:15m:2026-11-09T07:00:00:BUY"


def _fill(journal, command, trade_id, execution_id, lots, price, when, reason):
    journal.reducer.apply(ExecutionEvent(
        execution_id=execution_id, order_id=None, command_id=command, trade_id=trade_id,
        status=ExecutionStatus.FILL, filled_quantity=lots, price=D(price), fee=D("12.00"),
        timestamp=when, reason=reason, fee_source=FeeSource.CONFIGURED))


def _open(journal, trade_id=TRADE, fills=(("tinvest:exch-1", 1), ("tinvest:exch-2", 1)), when=NOW):
    plan = TradePlan(trade_id, "future-ng-macd", "NGX6", "BUY", f"signal-{trade_id}", D("2.861"), D("2.8"), (),
                     ProfileSnapshot("levels_rr", "1", {"buffer": D("1")}), when, "1m")
    lots = sum(part for _, part in fills)
    journal.manager.submit_plan(plan, OpenTrade("cmd-entry", trade_id, 0, "entry", lots),
                                price_step=D("0.001"), step_cost=D("1"))
    for execution_id, part in fills:
        _fill(journal, "cmd-entry", trade_id, execution_id, part, "2.861", when, "entry")


def _close(journal, trade_id, execution_id, lots, when):
    revision = journal.storage.connection.execute(
        "SELECT state_revision FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0]
    journal.manager.submit_action(CloseTrade("cmd-close", trade_id, revision, "raw-opposite-signal"))
    _fill(journal, "cmd-close", trade_id, execution_id, lots, "2.871", when, "raw-opposite-signal")


def _trade_op(op_id, trades, at=NOW, commission=None):
    return OperationReport(op_id, "", "trade", at, D("0"), None if commission is None else D(commission), tuple(trades))


def _fee_op(op_id, parent, amount, at=NOW):
    return OperationReport(op_id, parent, "fee", at, D(amount), None)


def _fees(journal):
    return {row[0]: (row[1], row[2]) for row in journal.storage.connection.execute(
        "SELECT execution_id, fee, fee_source FROM fills ORDER BY execution_id")}


def _sync(journal, api):
    return FeeSync(api, lambda: journal.storage.connection)


def _apply(journal, events):
    return sum(1 for event in events if journal.manager.consume(event))


def test_broker_fee_split_between_two_executions(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal)
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1), ("exch-2", 1)]), _fee_op("op-2", "op-1", "23.96")]
    events = _sync(journal, api).run(NOW + timedelta(minutes=1))
    assert [(e.execution_id, e.new_fee) for e in events] == [("tinvest:exch-1", D("11.98")),
                                                            ("tinvest:exch-2", D("11.98"))]
    assert events[0].adjustment_id == "opfee:broker_fee:op-1:tinvest:exch-1"
    assert _apply(journal, events) == 2
    assert _fees(journal) == {"tinvest:exch-1": ("11.98", "broker"), "tinvest:exch-2": ("11.98", "broker")}
    fees = journal.storage.connection.execute("SELECT fees FROM positions WHERE trade_id=?", (TRADE,)).fetchone()[0]
    assert D(fees) == D("23.96")


def test_split_remainder_goes_to_last_trade():
    pending = [SimpleNamespace(execution_id=f"tinvest:exch-{i}", trade_number=f"exch-{i}") for i in (1, 2, 3)]
    events = adjustments(pending, [_trade_op("op-1", [("exch-1", 1), ("exch-2", 1), ("exch-3", 1)]),
                                   _fee_op("op-2", "op-1", "10.00")], NOW)
    assert [e.new_fee for e in events] == [D("3.33"), D("3.33"), D("3.34")]


def test_commission_field_used_after_fifteen_minutes(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest:exch-1", 1),))
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1)], commission="11.98")]
    sync = _sync(journal, api)
    assert sync.run(NOW + timedelta(minutes=3)) == []          # свежая операция ждёт «комиссии брокера»
    events = sync.run(NOW + timedelta(minutes=20))
    assert [(e.adjustment_id, e.new_fee) for e in events] == [("opfee:commission:op-1:tinvest:exch-1", D("11.98"))]
    _apply(journal, events)
    assert _fees(journal)["tinvest:exch-1"] == ("11.98", "broker")


def test_equal_sum_still_marks_broker_source(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest:exch-1", 1),))
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1)]), _fee_op("op-2", "op-1", "12.00")]
    _apply(journal, _sync(journal, api).run(NOW))
    assert _fees(journal)["tinvest:exch-1"] == ("12.00", "broker")


def test_restart_after_adjustment_changes_nothing(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal)
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1), ("exch-2", 1)]), _fee_op("op-2", "op-1", "23.96")]
    events = _sync(journal, api).run(NOW)
    _apply(journal, events)
    assert _apply(journal, events) == 0                          # тот же ключ — повтор ничего не меняет
    api.calls.clear()
    assert _sync(journal, api).run(NOW + timedelta(minutes=5)) == []
    assert not [call for call in api.calls if call[0] == "operations"]   # ждущих нет — запроса нет


def test_other_executions_are_not_touched(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, fills=(("tinvest:exch-1", 1), ("tinvest-order:broker-1:2", 1)))
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1), ("exch-9", 1)]), _fee_op("op-2", "op-1", "24.00")]
    events = _sync(journal, api).run(NOW)
    assert [(e.execution_id, e.new_fee) for e in events] == [("tinvest:exch-1", D("12.00"))]


def test_old_executions_warned_once(tmp_path, caplog):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal, when=NOW - timedelta(days=8))
    api = FakeApi()
    sync = _sync(journal, api)
    with caplog.at_level(logging.WARNING, logger="src.broker_contour.fees"):
        assert sync.run(NOW) == []
        assert sync.run(NOW + timedelta(minutes=1)) == []
    warnings = [r for r in caplog.records if "старше 7 дней" in r.getMessage()]
    assert len(warnings) == 1 and "у 2 исполнений" in warnings[0].getMessage()
    assert not [call for call in api.calls if call[0] == "operations"]


def test_operations_failure_logs_type_only(tmp_path, caplog):
    journal = _Journal(tmp_path / "trades.sqlite3")
    _open(journal)
    api = FakeApi()
    api.operations_failure = OrderFailure("unknown", "UNAVAILABLE", "", "fake-secret-in-message")
    with caplog.at_level(logging.WARNING):
        assert _sync(journal, api).run(NOW) == []
    text = caplog.text
    assert "UNAVAILABLE" in text and "fake-secret-in-message" not in text
    assert _fees(journal)["tinvest:exch-1"] == ("12.00", "configured")


def test_sync_failure_does_not_stop_reconcile(caplog):
    class Broken:
        def run(self, now):
            raise RuntimeError("fake-secret-in-message")

    contour = SimpleNamespace(_holder={"fees": Broken()}, _clock=lambda: NOW)
    with caplog.at_level(logging.WARNING):
        BrokerContour._sync_fees(contour, SimpleNamespace(consume=lambda event: True))
    assert "RuntimeError" in caplog.text and "fake-secret-in-message" not in caplog.text


def test_performance_reports_broker_fees(tmp_path):
    path = tmp_path / "trades.sqlite3"
    journal = _Journal(path)
    _open(journal, fills=(("tinvest:exch-1", 1),))
    _close(journal, TRADE, "tinvest:exch-2", 1, NOW + timedelta(minutes=10))
    api = FakeApi()
    api.operation_items = [_trade_op("op-1", [("exch-1", 1)]), _fee_op("op-2", "op-1", "11.98"),
                           _trade_op("op-3", [("exch-2", 1)], at=NOW + timedelta(minutes=10)),
                           _fee_op("op-4", "op-3", "11.98", at=NOW + timedelta(minutes=10))]
    _apply(journal, _sync(journal, api).run(NOW + timedelta(minutes=11)))
    trading = ContourTrades(base=views.SANDBOX, journal=lambda: path, catalog=CATALOG,
                            clock=lambda: NOW + timedelta(minutes=30))
    performance = trading.performance({"period": ["all"]})
    assert performance["realized"]["fees_source"] == "broker"
    assert performance["realized"]["fees"] == "23.96"


# --- TInvestApi.operations ---------------------------------------------------------
def _money(value):
    q = decimal_to_quotation(D(value))
    return s.MoneyValue(currency="rub", units=q.units, nano=q.nano)


class _Gateway:
    def __init__(self, pages):
        self.pages, self.requests = list(pages), []

    def call(self, method, request):
        self.requests.append((method, request))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def test_api_operations_reads_pages_and_fields():
    executed = s.OperationState.OPERATION_STATE_EXECUTED
    buy = s.OperationItem(id="op-1", type=s.OperationType.OPERATION_TYPE_BUY, state=executed, date=NOW,
                          payment=_money("-24049"), commission=_money("-11.98"),
                          trades_info=s.OperationItemTrades(trades=[s.OperationItemTrade(num="exch-1", quantity=1)]))
    fee = s.OperationItem(id="op-2", parent_operation_id="op-1", type=s.OperationType.OPERATION_TYPE_BROKER_FEE,
                          state=executed, date=NOW, payment=_money("-11.98"),
                          commission=s.MoneyValue(currency="", units=0, nano=0))
    gateway = _Gateway([s.GetOperationsByCursorResponse(has_next=True, next_cursor="c-2", items=[buy]),
                        s.GetOperationsByCursorResponse(has_next=False, items=[fee])])
    result = TInvestApi(gateway, "acc-0000").operations(NOW - timedelta(hours=1), NOW)
    assert result == [
        OperationReport("op-1", "", "trade", NOW, D("24049"), D("11.98"), (("exch-1", 1),)),
        OperationReport("op-2", "op-1", "fee", NOW, D("11.98"), None, ()),
    ]
    assert gateway.requests[1][1].cursor == "c-2"
    assert gateway.requests[0][1].state == executed


def test_api_operations_failure_is_classified():
    gateway = _Gateway([BrokerCallError("operations.get_operations_by_cursor", "UNAVAILABLE", "", "fake")])
    try:
        TInvestApi(gateway, "acc-0000").operations(NOW - timedelta(hours=1), NOW)
    except OrderFailure as exc:
        assert exc.kind == "unknown"
    else:
        raise AssertionError("ожидался OrderFailure")
