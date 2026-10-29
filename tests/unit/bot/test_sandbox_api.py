"""Ресурс /api/v1/sandbox (add-sandbox-contour): команды, состав, сверка, схемы контракта."""

from __future__ import annotations

import json

import pytest

from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore, Member
from src.bot.launch_control import BadCommand, LaunchControl
from src.bot.sandbox_control import SandboxControl, parse_bindings, parse_sandbox_target
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from src.strategies.contracts import Assignment
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, INTERNAL_WORDS, _call, _keys, _problem
from tests.unit.clusters.test_report import _check

SHARES = {"SBER": [Assignment("share-sber-ma", "ma_cloud_rsi_macd", "ma_cloud", timeframe="1h")]}
FUTURES = {"NG": [Assignment("future-ng-macd", "macd_rsi_stoch", "levels_rr", timeframe="15m")]}


class FakeSupervisor:
    def __init__(self, directory):
        self.directory = directory
        self.kind = "sandbox"
        self.ready = True
        self.launched = []
        self.pid, self.exit_code, self.exited_at = None, None, None

    def snapshot(self):
        return {"pid": self.pid, "exit_code": self.exit_code, "exited_at": self.exited_at, "recovery": None,
                "started_at": "2026-10-30T09:10:02Z" if self.launched else None, "running": self.pid is not None}

    def launch(self, desired):
        self.launched.append(desired)
        self.pid = 9021
        return self.pid

    def busy_pid(self):
        return None


def _control(tmp_path, emulation=()):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    supervisor = FakeSupervisor(tmp_path / "contours" / "sandbox")
    control = SandboxControl(kind="sandbox", core_state=store, supervisor=supervisor, share_strategies=SHARES,
                             future_strategies=FUTURES, emulation_members=lambda: emulation)
    return control, store, supervisor


@pytest.mark.parametrize("body", [[], {}, {"mode": "pause"}, {"mode": "on", "extra": 1},
                                  {"mode": "no_entries", "bindings": ["x"]}, {"mode": "on", "bindings": [""]}])
def test_bad_target_bodies(body):
    with pytest.raises(BadCommand):
        parse_sandbox_target(body)


def test_target_and_bindings_parsing():
    assert parse_sandbox_target({"mode": "on"}) == ("on", None)
    assert parse_sandbox_target({"mode": "on", "bindings": ["a", "a", "b"]}) == ("on", ("a", "b"))
    assert parse_bindings({"bindings": ["a"], "mode": "close_all"}, remove=True) == (("a",), "close_all")
    with pytest.raises(BadCommand):
        parse_bindings({"bindings": ["a"], "mode": "close_all"}, remove=False)


def test_core_not_ready(tmp_path):
    control, _, supervisor = _control(tmp_path)
    supervisor.ready = False
    status, payload = control.put_target("on", ("future-ng-macd",), None)
    assert status == 409 and payload["code"] == "CORE_NOT_READY"


def test_unknown_binding_rejected(tmp_path):
    control, store, supervisor = _control(tmp_path)
    status, payload = control.put_target("on", ("share-xxx",), None)
    assert status == 422 and payload["rejections"] == [{"binding": "share-xxx", "reason": "Привязки нет в конфигурации."}]
    assert supervisor.launched == [] and store.desired("sandbox") is None


def test_start_records_target_and_launches(tmp_path):
    control, store, supervisor = _control(tmp_path)
    status, payload = control.put_target("on", ("future-ng-macd",), None)
    assert status == 202 and payload["target"] == {"mode": "on", "revision": 1}
    assert payload["state"] == "starting" and [b["id"] for b in payload["bindings"]] == ["future-ng-macd"]
    assert payload["launch"]["request"] == {"mode": "on", "bindings": ["future-ng-macd"]}
    assert supervisor.launched[0].members[0].instrument["instrument"] == "NG"
    assert control.put_target("on", None, None)[1]["code"] == "ALREADY_RUNNING"
    assert not INTERNAL_WORDS & _keys(payload)


def test_stop_without_process(tmp_path):
    control, _, _ = _control(tmp_path)
    assert control.put_target("no_entries", None, None)[1]["code"] == "NOT_RUNNING"


def test_bindings_change_without_process(tmp_path):
    ng = Member("NGZ6", {"ticker": "NGZ6", "short_name": "NG-12.26"}, {"name": "NG-12.26"})
    control, store, supervisor = _control(tmp_path, emulation=(ng,))
    status, payload = control.change_bindings(("future-ng-macd",), remove=False, mode="no_entries", expected=None)
    assert status == 202 and payload["revision"] == 1
    assert control.view()["target"] is None and control.view()["state"] == "waiting_command"
    trades = {item["id"]: item["trades_in"] for item in payload["available"]}
    assert trades == {"share-sber-ma": [], "future-ng-macd": ["emulation", "sandbox"]}
    assert control.change_bindings(("future-ng-macd",), remove=False, mode="no_entries", expected=None)[0] == 200
    status, payload = control.change_bindings(("share-sber-ma",), remove=True, mode="no_entries", expected=None)
    assert status == 422 and payload["rejections"][0]["reason"] == "Привязки нет в составе песочницы."
    # Запуск без поля bindings — с текущим составом.
    assert control.put_target("on", None, None)[0] == 202
    assert [m.key for m in supervisor.launched[0].members] == ["future-ng-macd"]


def test_current_state_from_status_and_reconcile(tmp_path):
    control, _, supervisor = _control(tmp_path)
    control.put_target("on", ("future-ng-macd",), None)
    supervisor.directory.mkdir(parents=True, exist_ok=True)
    write_status(supervisor.directory / "status.json", {
        "pid": 9021, "state": "running", "applied_generation": 1, "account": "…09c3",
        "capital": {"rub": "1000000.00", "liquid_portfolio": None}, "positions": [{"name": "NG-11.26", "lots": 1}],
        "orders": 0, "stop_orders": 0, "frozen": ["NG-11.26"], "last_reconcile_at": "2026-10-30T09:15:00Z",
        "account_link": "ok", "core_link": "ok", "reason": None, "members": [{"key": "future-ng-macd", "state": "watching"}]})
    BrokerStore(supervisor.directory / BROKER_DATABASE_FILE).add_report(
        {"at": "2026-10-30T09:15:00Z", "account": "…09c3", "result": "mismatch",
         "items": [{"instrument": "NG-11.26", "what": "position", "account": 1, "journal": 0}],
         "frozen": ["NG-11.26"], "skipped_in_row": 0})
    view = control.view()
    assert view["state"] == "running" and view["current"]["frozen"] == ["NG-11.26"]
    assert view["bindings"][0]["current"] == "watching"
    assert control.reconcile()["result"] == "mismatch"
    assert control.summary() == {"state": "running", "bindings": 1, "frozen": 1, "reconcile": "mismatch"}


@pytest.fixture
def sandbox_server(tmp_path):
    control, store, supervisor = _control(tmp_path)
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=LaunchControl(reply_timeout=2), control_token=CONTROL_KEY)
    srv.sandbox = control
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, control, supervisor
    srv.close()


def test_http_contract(sandbox_server):
    srv, _, supervisor = sandbox_server
    status, payload, headers = _call(srv, "GET", "/api/v1/sandbox", CONTROL_KEY)
    assert status == 200
    _check(payload, {"$ref": "#/components/schemas/Sandbox"})
    status, payload, headers = _call(srv, "PUT", "/api/v1/sandbox/target", CONTROL_KEY,
                                     {"mode": "on", "bindings": ["future-ng-macd"]})
    assert status == 202 and headers["Location"] == "/api/v1/sandbox" and headers["ETag"] == '"1"'
    _check(payload, {"$ref": "#/components/schemas/Sandbox"})
    status, payload, _ = _call(srv, "GET", "/api/v1/sandbox/bindings", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/BindingsView"})
    status, payload, _ = _call(srv, "GET", "/api/v1/sandbox/reconcile", CONTROL_KEY)
    assert status == 200 and payload is None
    status, payload, headers = _call(srv, "POST", "/api/v1/sandbox/bindings", CONTROL_KEY, {"bindings": ["share-xxx"]})
    _problem(status, payload, headers, "BINDINGS_REJECTED")
    assert payload["rejections"] == [{"binding": "share-xxx", "reason": "Привязки нет в конфигурации."}]
    status, payload, headers = _call(srv, "PUT", "/api/v1/sandbox/target", CONTROL_KEY, {"mode": "on"},
                                     if_match='"7"')
    _problem(status, payload, headers, "ALREADY_RUNNING")
    status, payload, _ = _call(srv, "GET", "/api/v1", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/Home"})
    assert payload["sandbox"]["state"] == "starting" and payload["_links"]["sandbox"] == {"href": "/api/v1/sandbox"}
    status, payload, _ = _call(srv, "GET", "/api/v1", EXPORT_KEY)
    assert payload["sandbox"] is None and "sandbox" not in payload["_links"]
    assert json.dumps(payload).find(CONTROL_KEY) == -1


def test_if_match_conflict(sandbox_server):
    srv, _, _ = sandbox_server
    _call(srv, "POST", "/api/v1/sandbox/bindings", CONTROL_KEY, {"bindings": ["future-ng-macd"]})
    status, payload, headers = _call(srv, "POST", "/api/v1/sandbox/bindings", CONTROL_KEY,
                                     {"bindings": ["share-sber-ma"]}, if_match='"5"')
    _problem(status, payload, headers, "REVISION_CHANGED")


# --- контракт привязки (add-tinvest-orders) ---------------------------------------

NG_PAYLOAD = {"label": "NG (Природный газ) — NG-12.26", "ticker": "NGZ6", "instrument_type": "future",
              "short_name": "NG-12.26", "uid": None, "family": "NG"}


def test_binding_instrument_resolved_or_rejected(tmp_path):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    supervisor = FakeSupervisor(tmp_path / "contours" / "sandbox")
    asked = []

    def resolve(card):
        asked.append(card["id"])
        return (NG_PAYLOAD, {"NGZ6": {"ticker": "NGZ6"}}) if card["instrument"] == "NG" else \
            "Нет торгуемого контракта семейства с экспирацией позже чем через 2 дня."

    control = SandboxControl(kind="sandbox", core_state=store, supervisor=supervisor, share_strategies=SHARES,
                             future_strategies=FUTURES, resolve_binding=resolve)
    status, payload = control.change_bindings(("share-sber-ma",), remove=False, mode="no_entries", expected=None)
    assert status == 422 and payload["rejections"][0]["reason"].startswith("Нет торгуемого контракта")
    assert store.desired("sandbox") is None
    assert control.put_target("on", ("future-ng-macd",), None)[0] == 202
    member = store.desired("sandbox").members[0]
    assert member.instrument["ticker"] == "NGZ6" and member.instrument["strategy"] == "macd_rsi_stoch"
    assert member.contract == {"NGZ6": {"ticker": "NGZ6"}}
    assert control.view()["bindings"][0]["instrument"] == "NG-12.26"


def test_current_shows_orders_mode_and_binding_states(tmp_path):
    control, _, supervisor = _control(tmp_path)
    control.put_target("on", ("future-ng-macd",), None)
    supervisor.directory.mkdir(parents=True, exist_ok=True)
    write_status(supervisor.directory / "status.json", {
        "pid": 9021, "state": "running", "applied_generation": 1, "orders_enabled": True, "trades": 1,
        "orders_in_flight": 2, "stream_link": "lost",
        "members": [{"key": "future-ng-macd", "state": "rejected", "reason": "Инструмент привязки не определён."}]})
    view = control.view()
    assert (view["current"]["orders_enabled"], view["current"]["trades"], view["current"]["orders_in_flight"],
            view["current"]["stream_link"]) == (True, 1, 2, "lost")
    assert view["bindings"][0]["current"] == "rejected" and view["bindings"][0]["reason"].startswith("Инструмент")
