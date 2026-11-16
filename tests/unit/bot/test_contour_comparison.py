"""Отчёт «Песочница против эмуляции» (add-account-vs-emulation-report): пары, расхождения, причины, API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.bot import control_views as views
from src.bot.comparison_view import ContourComparison, parse_signal
from src.bot.launch_control import LaunchControl
from src.bot.trades_view import ContourTrades, TradesError
from src.broker import ExecutionEvent, ExecutionStatus
from src.broker.port import FeeSource
from src.data.export_api import MarketDataExportServer
from src.trade_management.actions import OpenTrade
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan
from tests.unit.bot.test_contour_trades_api import CATALOG, _Journal, _Store
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.clusters.test_report import _check

UTC = timezone.utc
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
D = Decimal
SI = "future-si-levels"
NG = "future-ng-macd"
BR = "future-br-macd"


def _at(hour, minute=0, second=0):
    return datetime(2026, 11, 9, hour, minute, second, tzinfo=UTC)


def _signal(binding, ticker, hour, minute=0, side="BUY", tf="1h"):
    return f"{binding}:{ticker}:{tf}:2026-11-09T{hour:02d}:{minute:02d}:00:{side}"


class Pair:
    """Журналы эмуляции и песочницы, собранные штатными менеджером и редьюсером."""

    def __init__(self, tmp_path):
        (tmp_path / "emulation").mkdir()
        (tmp_path / "sandbox").mkdir()
        self.paths = {"emulation": tmp_path / "emulation" / "trades.sqlite3",
                      "sandbox": tmp_path / "sandbox" / "trades.sqlite3"}
        self.journals = {name: _Journal(path) for name, path in self.paths.items()}
        self.members = {SI, NG}

    def __getitem__(self, name):
        return self.journals[name]

    def reject_signal(self, contour, signal, reason):
        self.journals[contour].storage.connection.execute(
            "INSERT INTO calculations(calculation_id, assignment_id, signal_id, algorithm, algorithm_version, "
            "input_json, steps_json, rounding_json, output_json, outcome, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"calc-{signal}", signal.split(":")[0], f"signal-{signal}", "entry.gate", "1", "{}", "[]", "{}", "{}", "REJECTED",
             reason, NOW.isoformat()))
        self.journals[contour].storage.connection.commit()

    def unfilled(self, contour, signal, binding, ticker, when, *, status=None, reason=None):
        """Сделка без исполнения входа: отказ или отмена брокера, либо вход ещё ждёт."""
        journal = self.journals[contour]
        plan = TradePlan(signal, binding, ticker, "BUY", f"signal-{signal}", D("100"), D("90"), (),
                         ProfileSnapshot("levels_rr", "1", {"buffer": D("1")}), when, "1m")
        journal.manager.submit_plan(plan, OpenTrade(f"cmd-{signal}", signal, 0, "entry", 1),
                                    price_step=D("1"), step_cost=D("1"))
        if status is not None:
            journal.reducer.apply(ExecutionEvent(
                execution_id=f"out-{signal}", order_id=None, command_id=f"cmd-{signal}", trade_id=signal,
                status=status, filled_quantity=0, price=None, fee=D("0"), timestamp=when, reason=reason,
                fee_source=FeeSource.UNKNOWN))

    def report(self, query=None, members=None):
        for journal in self.journals.values():
            journal.storage.connection.commit()
        comparison = _comparison(self.paths["emulation"], self.paths["sandbox"],
                                 self.members if members is None else members)
        return comparison.report(query or {})


def _trades(base, path):
    return ContourTrades(base=base, journal=lambda: path, catalog=CATALOG, clock=lambda: NOW)


def _comparison(emulation_path, sandbox_path, members=frozenset()):
    return ContourComparison(emulation=_trades(views.EMULATION, emulation_path),
                             sandbox=_trades(views.SANDBOX, sandbox_path), members=lambda: set(members),
                             catalog=CATALOG, clock=lambda: NOW)


def _item(report, kind=None):
    items = [item for item in report["items"] if kind is None or item["kind"] == kind]
    assert len(items) == 1, report["items"]
    return items[0]


def _codes(item):
    return {diff["code"]: diff["value"] for diff in item["diffs"]}


@pytest.fixture
def pair(tmp_path):
    return Pair(tmp_path)


def _schema(payload):
    _check(payload, {"$ref": "#/components/schemas/Comparison"})


# --- ключ сигнала ----------------------------------------------------------------
def test_signal_key_is_same_in_both_contours():
    """Ключ живого анализа одинаков в эмуляции и песочнице: привязка, тикер, ТФ, бар, сторона."""
    import pandas as pd

    from src.instruments.model import instrument_key

    binding = type("B", (), {"id": "future-si-ma-raw"})()
    instrument = type("I", (), {"ticker": "SiZ6", "kind": "future"})()
    close = pd.Timestamp("2026-11-06T04:25:00")
    keys = {f"{binding.id}:{instrument_key(instrument)}:5m:{close.isoformat()}:SELL" for _ in ("emulation", "sandbox")}
    assert len(keys) == 1
    parsed = parse_signal(keys.pop())
    assert parsed == {"binding": "future-si-ma-raw", "ticker": parsed["ticker"], "timeframe": "5m", "side": "SELL"}


def test_parse_signal_keeps_bar_time_colons():
    assert parse_signal("future-si-ma-raw:SiZ6:5m:2026-11-06T04:25:00:SELL") == {
        "binding": "future-si-ma-raw", "ticker": "SiZ6", "timeframe": "5m", "side": "SELL"}


# --- пары ------------------------------------------------------------------------
def test_entry_worse_by_two_steps(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92410", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "BUY", 1, "92412", _at(7, 15, 20))
    item = _item(pair.report())
    assert item["kind"] == "pair" and item["steps_unknown"] is False
    assert _codes(item) == {"entry_price": "2"}
    assert item["signal"] == {"binding": {"id": SI, "strategy": "levels", "timeframe": "1h"}, "instrument": "Si-12.26",
                              "timeframe": "1h", "side": "long", "at": "2026-11-09T07:15:00Z"}
    assert item["sandbox"]["state"] == "open" and item["sandbox"]["exit"] is None


def test_broker_stop_against_minute_stop(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92410", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "BUY", 1, "92410", _at(7, 15))
    pair["emulation"].stop(signal, 1, "92296", _at(10, 15))
    pair["sandbox"].stop(signal, 1, "92300", _at(10, 14, 37))
    item = _item(pair.report())
    assert _codes(item) == {"exit_price": "-4"}
    assert item["emulation"]["exit"]["kind"] == item["sandbox"]["exit"]["kind"] == "stop"
    summary = pair.report()["summary"]
    assert summary["exit_steps"] == {"count": 1, "mean": "-4", "max_abs": "4"}
    assert summary["entry_steps"] == {"count": 1, "mean": "0", "max_abs": "0"}


def test_short_entry_sign(pair):
    signal = _signal(SI, "SIZ6", 7, side="SELL")
    pair["emulation"].open(signal, SI, "SIZ6", "SELL", 1, "92410", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "SELL", 1, "92407", _at(7, 15))
    assert _codes(_item(pair.report())) == {"entry_price": "3"}   # продали дешевле — хуже


def test_different_exit_kind(pair):
    signal = _signal(NG, "NGX6", 9)
    target = (TargetPlan("tp-1", D("2.884"), D("1")),)
    for name in ("emulation", "sandbox"):
        pair[name].open(signal, NG, "NGX6", "BUY", 1, "2.861", _at(9, 5), step="0.001", targets=target)
    pair["emulation"].target(signal, "tp-1", 1, "2.884", _at(10, 20))
    pair["sandbox"].stop(signal, 1, "2.820", _at(10, 20))
    item = _item(pair.report())
    assert _codes(item) == {"exit_kind": {"emulation": "target", "sandbox": "stop"}}


def test_different_lots_and_net_per_lot(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 3, "92400", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair["emulation"].close(signal, 3, "92700", _at(8), "raw-opposite-signal")
    pair["sandbox"].close(signal, 1, "92690", _at(8), "raw-opposite-signal")
    item = _item(pair.report())
    assert _codes(item) == {"lots": {"emulation": 3, "sandbox": 1}, "exit_price": "10"}
    assert item["emulation"]["net_per_lot"] == "299.33"         # (900 − 2 комиссии по 1) / 3
    assert item["sandbox"]["net_per_lot"] == "288.00"
    result = pair.report()["summary"]["paired_result"]
    assert result["count"] == 1 and result["sandbox"]["net_per_lot"] == "288.00"


def test_entry_and_exit_time_gaps(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 17))
    pair["emulation"].close(signal, 1, "92500", _at(8), "raw-opposite-signal")
    pair["sandbox"].close(signal, 1, "92500", _at(8, 0, 45), "raw-opposite-signal")
    assert _codes(_item(pair.report())) == {"entry_time": 120}


def test_closed_one_side(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair["sandbox"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair["emulation"].close(signal, 1, "92500", _at(8), "raw-opposite-signal")
    assert _codes(_item(pair.report())) == {"closed_one_side": "emulation"}


def test_pair_without_diffs(pair):
    signal = _signal(SI, "SIZ6", 7)
    for name in ("emulation", "sandbox"):
        pair[name].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
        pair[name].close(signal, 1, "92500", _at(8), "raw-opposite-signal")
    item = _item(pair.report())
    assert item["diffs"] == [] and item["steps_unknown"] is False


def test_steps_unknown_without_price_step(pair):
    signal = _signal(SI, "SIZ6", 7)
    for name in ("emulation", "sandbox"):
        pair[name].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
        pair[name].storage.connection.execute("UPDATE trades SET price_step=NULL")
    pair["sandbox"].storage.connection.execute("UPDATE fills SET price='92450'")
    item = _item(pair.report())
    assert item["steps_unknown"] is True and item["diffs"] == []


# --- без пары --------------------------------------------------------------------
def test_sandbox_entry_blocked(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair.reject_signal("sandbox", signal, "contour-stopping")
    item = _item(pair.report())
    assert item["kind"] == "emulation_only" and item["sandbox"] is None
    assert item["missing_reason"]["contour"] == "sandbox" and item["missing_reason"]["code"] == "contour-stopping"


def test_sandbox_broker_reject_is_not_a_side(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair.unfilled("sandbox", signal, SI, "SIZ6", _at(7, 15), status=ExecutionStatus.REJECT, reason="broker:30042")
    item = _item(pair.report())
    assert item["kind"] == "emulation_only"
    assert item["missing_reason"]["code"] == "broker:30042"


def test_sandbox_limit_entry_cancelled(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair.unfilled("sandbox", signal, SI, "SIZ6", _at(7, 15), status=ExecutionStatus.CANCEL,
                  reason="limit-entry-expired")
    assert _item(pair.report())["missing_reason"]["code"] == "limit-entry-expired"


def test_sandbox_entry_still_pending(pair):
    signal = _signal(SI, "SIZ6", 7)
    pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    pair.unfilled("sandbox", signal, SI, "SIZ6", _at(7, 15))
    reason = _item(pair.report())["missing_reason"]
    assert reason["code"] == "entry-pending" and reason["label"] == "вход ещё не исполнен"


def test_binding_missing_in_emulation_is_not_seen(pair):
    signal = _signal(NG, "NGX6", 9)
    pair["sandbox"].open(signal, NG, "NGX6", "BUY", 1, "2.861", _at(9, 5), step="0.001")
    item = _item(pair.report())
    assert item["kind"] == "sandbox_only"
    assert item["missing_reason"] == {"contour": "emulation", "code": "not-seen", "label": "сигнала в контуре не было"}


def test_emulation_bindings_outside_sandbox_excluded(pair):
    pair["emulation"].open(_signal(BR, "BRZ6", 8), BR, "BRZ6", "BUY", 1, "64.38", _at(8), step="0.01")
    pair["emulation"].open(_signal(SI, "SIZ6", 7), SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    report = pair.report(members={SI})
    assert [item["signal"]["binding"]["id"] for item in report["items"]] == [SI]


def test_removed_binding_with_sandbox_trades_stays(pair):
    signal = _signal(NG, "NGX6", 9)
    pair["sandbox"].open(signal, NG, "NGX6", "BUY", 1, "2.861", _at(9, 5), step="0.001")
    pair["emulation"].open(signal, NG, "NGX6", "BUY", 1, "2.861", _at(9, 5), step="0.001")
    assert _item(pair.report(members=set()))["kind"] == "pair"


# --- сводка, порядок, фильтры, ошибки -----------------------------------------------
def _week(pair):
    for hour in range(1, 8):
        signal = _signal(SI, "SIZ6", hour)
        for name in ("emulation", "sandbox"):
            pair[name].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(hour, 5))
            pair[name].close(signal, 1, "92500", _at(hour, 30), "raw-opposite-signal")
    for hour in (8, 9):
        signal = _signal(SI, "SIZ6", hour)
        pair["emulation"].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(hour, 5))
        pair["emulation"].close(signal, 1, "92450", _at(hour, 30), "raw-opposite-signal")
        pair.reject_signal("sandbox", signal, "risk-budget")
    signal = _signal(NG, "NGX6", 10)
    pair["sandbox"].open(signal, NG, "NGX6", "BUY", 1, "2.861", _at(10, 5), step="0.001")


def test_summary_counts_whole_period_with_limit(pair):
    _week(pair)
    report = pair.report({"period": ["week"], "limit": ["5"]})
    _schema(report)
    assert len(report["items"]) == 5 and report["earlier"] is True
    summary = report["summary"]
    assert (summary["pairs"], summary["emulation_only"], summary["sandbox_only"]) == (7, 2, 1)
    assert [(entry["contour"], entry["code"], entry["count"]) for entry in summary["missing_reasons"]] == [
        ("sandbox", "risk-budget", 2), ("emulation", "not-seen", 1)]
    assert summary["paired_result"]["count"] == 7
    assert summary["sandbox_fees"] == {"broker": 0, "estimated": 15}
    ats = [item["signal"]["at"] for item in report["items"]]
    assert ats == sorted(ats, reverse=True)


def test_instrument_filter_applies_to_summary(pair):
    _week(pair)
    report = pair.report({"period": ["week"], "instrument": ["ng-11.26"]})
    assert report["summary"]["sandbox_only"] == 1 and report["summary"]["pairs"] == 0
    report = pair.report({"period": ["week"], "binding": [SI]})
    assert report["summary"]["sandbox_only"] == 0 and report["summary"]["pairs"] == 7


def test_period_by_signal_time(pair):
    signal = _signal(SI, "SIZ6", 7)
    for name in ("emulation", "sandbox"):
        pair[name].open(signal, SI, "SIZ6", "BUY", 1, "92400", _at(7, 15))
    report = pair.report({"from": ["2026-11-09T08:00:00Z"], "to": ["2026-11-09T12:00:00Z"]})
    assert report["items"] == [] and report["period"]["name"] == "custom"


def test_no_journals_empty_report(tmp_path):
    report = _comparison(tmp_path / "none-e.sqlite3", tmp_path / "none-s.sqlite3", {SI}).report({"period": ["week"]})
    _schema(report)
    assert report["items"] == [] and report["summary"]["pairs"] == 0 and report["summary"]["entry_steps"] is None


@pytest.mark.parametrize("query", [{"limit": ["0"]}, {"period": ["year"]}, {"unknown": ["1"]},
                                   {"binding": ["no-such-binding"]}, {"period": ["day"], "from": ["2026-11-09T00:00:00Z"]}])
def test_bad_queries(pair, query):
    with pytest.raises(TradesError) as caught:
        pair.report(query)
    assert caught.value.code == "INVALID_REQUEST"


def test_no_tickers_in_report(pair):
    _week(pair)
    text = json.dumps(pair.report({"period": ["week"]}), ensure_ascii=False)
    assert "SIZ6" not in text and "NGX6" not in text


# --- HTTP ------------------------------------------------------------------------
class TestHttp:
    @pytest.fixture
    def server(self, pair):
        _week(pair)
        for journal in pair.journals.values():
            journal.storage.connection.commit()
        srv = MarketDataExportServer(_Store(), EXPORT_KEY, port=0, control=LaunchControl(reply_timeout=2),
                                     control_token=CONTROL_KEY)
        srv.comparison = _comparison(pair.paths["emulation"], pair.paths["sandbox"], {SI, NG})
        try:
            srv.start()
        except PermissionError:
            pytest.skip("Песочница не разрешает открыть loopback-сокет")
        yield srv
        srv.close()

    def test_route_matches_schema(self, server):
        status, payload, _ = _call(server, "GET", "/api/v1/sandbox/comparison?period=week", CONTROL_KEY)
        assert status == 200, payload
        _schema(payload)
        assert payload["_links"]["self"]["href"] == "/api/v1/sandbox/comparison?period=week"

    def test_bad_request_is_problem(self, server):
        _problem(400, *_call(server, "GET", "/api/v1/sandbox/comparison?limit=0", CONTROL_KEY)[1:], "INVALID_REQUEST")

    def test_export_key_rejected(self, server):
        status, payload, headers = _call(server, "GET", "/api/v1/sandbox/comparison", EXPORT_KEY)
        _problem(401, payload, headers, "UNAUTHORIZED")
