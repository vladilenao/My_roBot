"""Ресурс ``/api/v1/account`` (add-account-contour): чтение, запуск, остановка с подтверждением, состав до Р2."""

from __future__ import annotations

import pytest

from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore
from src.bot.account_control import AccountControl, parse_account_target
from src.bot.launch_control import BadCommand, LaunchControl
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.bot.test_sandbox_api import FUTURES, SHARES, FakeSupervisor
from tests.unit.clusters.test_report import _check

ADMISSION = {"access_level": "read_only", "qualified_for_work_with": ["derivative"], "derivatives": True,
             "tariff": "trader", "limits": {"orders": 100}, "warnings": []}


def _control(tmp_path):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    supervisor = FakeSupervisor(tmp_path / "contours" / "account")
    supervisor.kind = "account"
    control = AccountControl(kind="account", core_state=store, supervisor=supervisor, share_strategies=SHARES,
                             future_strategies=FUTURES)
    return control, store, supervisor


@pytest.mark.parametrize("body", [[], {"mode": "pause"}, {"mode": "on", "bindings": []},
                                  {"mode": "on", "extra": 1}, {"mode": "close_all", "confirm": "yes"}])
def test_bad_account_target_bodies(body):
    with pytest.raises(BadCommand):
        parse_account_target(body)


def test_account_target_parsing():
    assert parse_account_target({"mode": "on"}) == ("on", False)
    assert parse_account_target({"mode": "close_all", "confirm": True}) == ("close_all", True)


@pytest.fixture
def account_server(tmp_path):
    control, store, supervisor = _control(tmp_path)
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=LaunchControl(reply_timeout=2), control_token=CONTROL_KEY)
    srv.account = control
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, control, supervisor
    srv.close()


def test_http_contract(account_server, tmp_path):
    srv, _, supervisor = account_server
    status, payload, _ = _call(srv, "GET", "/api/v1/account", CONTROL_KEY)
    assert status == 200 and payload["state"] == "waiting_command"
    _check(payload, {"$ref": "#/components/schemas/Account"})
    assert [action["name"] for action in payload["_actions"]] == ["start"]
    assert "comparison" not in payload["_links"] and payload["_links"]["positions"] == {"href": "/api/v1/account/positions"}

    status, payload, headers = _call(srv, "PUT", "/api/v1/account/target", CONTROL_KEY, {"mode": "on"})
    assert status == 202 and headers["Location"] == "/api/v1/account" and supervisor.launched
    _check(payload, {"$ref": "#/components/schemas/Account"})

    status, payload, headers = _call(srv, "PUT", "/api/v1/account/target", CONTROL_KEY, {"mode": "on", "bindings": []})
    _problem(status, payload, headers, "INVALID_REQUEST")
    status, payload, headers = _call(srv, "PUT", "/api/v1/account/target", CONTROL_KEY, {"mode": "close_all"})
    _problem(status, payload, headers, "CONFIRM_REQUIRED")
    status, payload, headers = _call(srv, "POST", "/api/v1/account/bindings", CONTROL_KEY, {"bindings": ["future-ng-macd"]})
    _problem(status, payload, headers, "ACCOUNT_READ_ONLY")
    status, payload, headers = _call(srv, "POST", "/api/v1/account/guards/reset", CONTROL_KEY, {"guards": ["broker_rejects"]})
    assert status == 404

    status, payload, _ = _call(srv, "GET", "/api/v1/account/bindings", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/BindingsView"})
    assert payload["members"] == [] and payload["_actions"] == []

    status, payload, _ = _call(srv, "PUT", "/api/v1/account/target", CONTROL_KEY, {"mode": "close_all", "confirm": True})
    assert status == 202 and payload["target"]["mode"] == "close_all"

    status, payload, _ = _call(srv, "GET", "/api/v1", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/Home"})
    assert payload["_links"]["account"] == {"href": "/api/v1/account"} and payload["account"]["bindings"] == 0
    status, payload, _ = _call(srv, "GET", "/api/v1", EXPORT_KEY)
    assert payload["account"] is None and "account" not in payload["_links"]


def test_admission_and_reconcile_with_fee_probes(account_server, tmp_path):
    srv, control, supervisor = account_server
    directory = tmp_path / "contours" / "account"
    BrokerStore(directory / BROKER_DATABASE_FILE).add_report(
        {"at": "2026-11-12T07:00:00Z", "account": "…7f21", "result": "ok", "items": [], "in_flight": [],
         "frozen": [], "skipped_in_row": 0})
    from src.account.fee_probes import FeeProbes

    FeeProbes(directory / BROKER_DATABASE_FILE)._save(
        __import__("datetime").datetime(2026, 11, 12, 7, 15, tzinfo=__import__("datetime").timezone.utc),
        {"instrument": "SBER", "operation": "…9a8b", "trades_info": True, "broker_fee_linked": False,
         "broker_fee": None, "commission": "3.10", "error": None})
    supervisor.launched.append(None)
    supervisor.pid = 777
    write_status(directory / "status.json", {"state": "running", "pid": 777, "account": "…7f21",
                                             "capital": {"rub": "150000.00", "liquid_portfolio": "150000.00"},
                                             "orders_enabled": False, "admission": ADMISSION})
    status, payload, _ = _call(srv, "GET", "/api/v1/account", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/Account"})
    assert payload["current"]["admission"] == ADMISSION and payload["current"]["orders_enabled"] is False
    status, payload, _ = _call(srv, "GET", "/api/v1/account/reconcile", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/AccountReconcileReport"})
    assert payload["fee_probes"][0]["instrument"] == "SBER" and payload["fee_probes"][0]["commission"] == "3.10"
