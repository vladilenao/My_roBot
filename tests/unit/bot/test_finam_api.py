"""Ресурс ``/api/v1/finam`` (add-finam-contour, решение 13; tasks 5.6): тот же ресурс, что у «Счёта», свои части."""

from __future__ import annotations

import pytest

from src.account.fee_probes import fee_probe_extras
from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore
from src.bot import control_views as views
from src.bot.account_control import AccountControl
from src.bot.launch_control import LaunchControl
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.bot.test_sandbox_api import FUTURES, SHARES, FakeSupervisor
from tests.unit.clusters.test_report import _check

FINAM_ADMISSION = {"access_level": "read_only", "limits": {"GetAccount": 200, "GetOrders": 200}, "warnings": []}


def _control(tmp_path, kind, **parts):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    supervisor = FakeSupervisor(tmp_path / "contours" / kind)
    supervisor.kind = kind
    control = AccountControl(kind=kind, core_state=store, supervisor=supervisor, share_strategies=SHARES,
                             future_strategies=FUTURES, **parts)
    return control, supervisor


@pytest.fixture
def servers(tmp_path):
    """«Счёт» и «Счёт Финам» в одном API — как собирает run.py."""
    account, _ = _control(tmp_path, "account", provider="tinvest", reconcile_extras=fee_probe_extras)
    finam, supervisor = _control(tmp_path, "finam", base=views.FINAM, title="Счёт Финам", provider="finam",
                                 comparison=False, guards=False)
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=LaunchControl(reply_timeout=2), control_token=CONTROL_KEY)
    srv.live = {views.ACCOUNT: account, views.FINAM: finam}
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, finam, supervisor
    srv.close()


def test_finam_resource_reads_and_start(servers):
    srv, _, supervisor = servers
    status, payload, _ = _call(srv, "GET", "/api/v1/finam", CONTROL_KEY)
    assert status == 200 and payload["broker"] == "finam"
    _check(payload, {"$ref": "#/components/schemas/Finam"})
    assert [action["name"] for action in payload["_actions"]] == ["start"]
    assert payload["_actions"][0]["title"] == "Запустить «Счёт Финам»"
    assert payload["_actions"][0]["href"] == "/api/v1/finam/target"
    assert "comparison" not in payload["_links"] and payload["_links"]["positions"] == {"href": "/api/v1/finam/positions"}

    status, payload, headers = _call(srv, "PUT", "/api/v1/finam/target", CONTROL_KEY, {"mode": "on"})
    assert status == 202 and headers["Location"] == "/api/v1/finam" and supervisor.launched
    _check(payload, {"$ref": "#/components/schemas/Finam"})


def test_finam_composition_is_read_only(servers):
    srv, _, _ = servers
    status, payload, headers = _call(srv, "POST", "/api/v1/finam/bindings", CONTROL_KEY,
                                     {"bindings": ["future-ng-macd"]})
    _problem(status, payload, headers, "ACCOUNT_READ_ONLY")
    assert "[contours.finam] orders = false" in payload["detail"]
    status, payload, headers = _call(srv, "PUT", "/api/v1/finam/target", CONTROL_KEY, {"mode": "on", "bindings": []})
    _problem(status, payload, headers, "ACCOUNT_READ_ONLY")
    status, payload, headers = _call(srv, "PUT", "/api/v1/finam/target", CONTROL_KEY, {"mode": "close_all"})
    _problem(status, payload, headers, "CONFIRM_REQUIRED")


@pytest.mark.parametrize("method, path", [("GET", "/api/v1/finam/comparison"),
                                          ("POST", "/api/v1/finam/guards/reset")])
def test_finam_has_no_comparison_and_guards(servers, method, path):
    srv, _, _ = servers
    status, payload, _ = _call(srv, method, path, CONTROL_KEY, {"guards": ["broker_rejects"]} if method == "POST" else None)
    assert status == 404


def test_finam_admission_and_plain_reconcile(servers, tmp_path):
    srv, _, supervisor = servers
    directory = tmp_path / "contours" / "finam"
    BrokerStore(directory / BROKER_DATABASE_FILE).add_report(
        {"at": "2026-11-25T09:00:00Z", "account": "…3322", "result": "ok", "items": [], "in_flight": [],
         "frozen": [], "skipped_in_row": 0})
    supervisor.launched.append(None)
    supervisor.pid = 778
    write_status(directory / "status.json", {"state": "running", "pid": 778, "account": "…3322", "broker": "finam",
                                             "capital": {"rub": "30000.00", "liquid_portfolio": "30000.00"},
                                             "account_link": "maintenance", "orders_enabled": False,
                                             "admission": FINAM_ADMISSION})
    status, payload, _ = _call(srv, "GET", "/api/v1/finam", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/Finam"})
    assert payload["current"]["admission"] == FINAM_ADMISSION and payload["current"]["account_link"] == "maintenance"
    status, payload, _ = _call(srv, "GET", "/api/v1/finam/reconcile", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/ReconcileReport"})
    assert payload["result"] == "ok" and "fee_probes" not in payload


def test_home_lists_finam_and_brokers(servers):
    srv, _, _ = servers
    status, payload, _ = _call(srv, "GET", "/api/v1", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/Home"})
    assert payload["_links"]["finam"] == {"href": "/api/v1/finam"}
    assert payload["finam"]["broker"] == "finam" and payload["account"]["broker"] == "tinvest"
    status, payload, _ = _call(srv, "GET", "/api/v1", EXPORT_KEY)
    assert "finam" not in payload["_links"] and payload["account"] is None


def test_finam_trading_routes_and_unknown(servers):
    srv, _, _ = servers
    status, payload, _ = _call(srv, "GET", "/api/v1/finam/bindings", CONTROL_KEY)
    _check(payload, {"$ref": "#/components/schemas/BindingsView"})
    assert payload["_links"]["self"] == {"href": "/api/v1/finam/bindings"} and payload["_actions"] == []
    status, _, _ = _call(srv, "GET", "/api/v1/finam/unknown", CONTROL_KEY)
    assert status == 404


def test_account_without_resource_is_unavailable(tmp_path):
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=LaunchControl(reply_timeout=2), control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        status, payload, _ = _call(srv, "GET", "/api/v1/finam", CONTROL_KEY)
        assert status == 404 and payload["detail"] == "Контур недоступен в этом процессе."
    finally:
        srv.close()
