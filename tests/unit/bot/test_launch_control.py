import json
import threading
from urllib import error, request

import pytest

from src.bot.launch import Chosen, Rejection, SOURCE_API, SOURCE_CLI
from src.bot.launch_control import BadCommand, LaunchControl, parse_command
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from tests.unit.clusters.test_report import SPEC, _check

CONTROL_KEY = "control-test-key"
EXPORT_KEY = "export-test-key"
NG = Chosen("NG", "NG-12.26", "future", 52, "NG", True)


class TestParseCommand:
    def test_default_is_all(self):
        assert parse_command({"kind": "emulation"}).instruments == ("ALL",)

    def test_tokens(self):
        assert parse_command({"kind": "emulation", "instruments": ["ng", "SBER"]}).source == SOURCE_API

    @pytest.mark.parametrize("body, code", [
        ([], "INVALID_REQUEST"),
        ({"kind": "emulation", "extra": 1}, "INVALID_REQUEST"),
        ({"kind": "history"}, "UNSUPPORTED_KIND"),
        ({"kind": "emulation", "instruments": []}, "INVALID_REQUEST"),
        ({"kind": "emulation", "instruments": ["NG", 5]}, "INVALID_REQUEST"),
        ({"kind": "emulation", "instruments": ["all", "NG"]}, "INVALID_REQUEST"),
    ])
    def test_bad_bodies(self, body, code):
        with pytest.raises(BadCommand) as exc:
            parse_command(body)
        assert exc.value.code == code


def _main_thread(control, outcome):
    """Имитация главного потока робота: принять одну команду и ответить."""
    def run():
        pending = control.next_command(poll_secs=0.05)
        if outcome == "reject":
            control.reject(pending, [Rejection("AFLT", "У инструмента нет привязок в robot.toml.")])
        else:
            control.mark_running(SOURCE_API, [NG], "--mode emulation --instruments NG-12.26", pending)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class TestLaunchControl:
    def test_command_is_answered_by_main_thread(self):
        control = LaunchControl()
        control.accept_commands()
        _main_thread(control, "run")
        status, payload = control.submit(parse_command({"kind": "emulation", "instruments": ["NG"]}))
        assert status == 201 and payload["state"] == "running" and payload["source"] == "api"
        assert control.submit(parse_command({"kind": "emulation"}))[0] == 409

    def test_rejection_keeps_waiting(self):
        control = LaunchControl()
        control.accept_commands()
        _main_thread(control, "reject")
        status, payload = control.submit(parse_command({"kind": "emulation", "instruments": ["AFLT"]}))
        assert status == 422 and payload["rejections"][0]["token"] == "AFLT"
        snapshot = control.snapshot()
        assert snapshot["state"] == "waiting_command" and snapshot["rejections"]

    def test_slow_resolution_answers_202(self):
        control = LaunchControl(reply_timeout=0.05)
        control.accept_commands()
        status, payload = control.submit(parse_command({"kind": "emulation"}))
        assert status == 202 and payload["state"] == "starting"

    def test_not_waiting_means_conflict(self):
        control = LaunchControl()
        control.mark_running(SOURCE_CLI, [NG], "--mode emulation --instruments NG-12.26")
        status, payload = control.submit(parse_command({"kind": "emulation"}))
        assert status == 409 and payload["contour"]["source"] == "cli"


@pytest.fixture
def server(tmp_path):
    control = LaunchControl(reply_timeout=2)
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=control, control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, control
    srv.close()


def _call(srv, method, path, key, body=None, host=None):
    address, port = srv.address
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = request.Request(f"http://{address}:{port}{path}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


class TestContoursApi:
    def test_waiting_state_matches_contract(self, server):
        srv, control = server
        control.accept_commands()
        status, payload, headers = _call(srv, "GET", "/api/v1/contours", CONTROL_KEY)
        assert status == 200
        _check(payload, {"$ref": "#/components/schemas/ContourList"})
        assert payload["contours"][0]["state"] == "waiting_command"
        assert not any(name.lower().startswith("access-control") for name in headers)

    def test_start_then_conflict(self, server):
        srv, control = server
        control.accept_commands()
        _main_thread(control, "run")
        status, payload, _ = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, {"kind": "emulation", "instruments": ["NG"]})
        assert status == 201
        _check(payload, {"$ref": "#/components/schemas/Contour"})
        assert payload["instruments"][0] == {"token": "NG", "name": "NG-12.26", "instrument_type": "future", "days_to_expiry": 52}
        status, payload, _ = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, {"kind": "emulation"})
        assert status == 409
        _check(payload, {"$ref": "#/components/schemas/ContourConflict"})

    def test_rejected_command(self, server):
        srv, control = server
        control.accept_commands()
        _main_thread(control, "reject")
        status, payload, _ = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, {"kind": "emulation", "instruments": ["AFLT"]})
        assert status == 422
        _check(payload, {"$ref": "#/components/schemas/LaunchRejected"})

    def test_slow_command_points_to_state(self, server):
        srv, control = server
        control._reply_timeout = 0.05
        control.accept_commands()
        status, payload, headers = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, {"kind": "emulation"})
        assert status == 202 and headers["Location"] == "/api/v1/contours"

    @pytest.mark.parametrize("body, code", [(b"{bad", "INVALID_REQUEST"), ({"kind": "history"}, "UNSUPPORTED_KIND")])
    def test_bad_body(self, server, body, code):
        srv, control = server
        control.accept_commands()
        status, payload, _ = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, body)
        assert status == 400 and payload["code"] == code

    def test_export_key_cannot_control_and_control_key_cannot_export(self, server):
        srv, _ = server
        assert _call(srv, "GET", "/api/v1/contours", EXPORT_KEY)[0] == 401
        assert _call(srv, "POST", "/api/v1/contours", EXPORT_KEY, {"kind": "emulation"})[0] == 401
        assert _call(srv, "GET", "/api/v1/market-data/source", CONTROL_KEY)[0] == 401

    def test_foreign_host_forbidden(self, server):
        srv, _ = server
        status, payload, _ = _call(srv, "POST", "/api/v1/contours", CONTROL_KEY, {"kind": "emulation"},
                                   host=f"evil.example:{srv.address[1]}")
        assert status == 403 and payload["code"] == "FORBIDDEN_HOST"

    def test_key_never_returned(self, server):
        srv, control = server
        control.accept_commands()
        for path in ("/api/v1/contours", "/openapi.yaml"):
            address, port = srv.address
            req = request.Request(f"http://{address}:{port}{path}", headers={"Authorization": f"Bearer {CONTROL_KEY}"})
            with request.urlopen(req, timeout=5) as response:
                assert CONTROL_KEY not in response.read().decode()


def test_server_with_only_control_key(tmp_path):
    control = LaunchControl()
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), None, port=0,
                                 control=control, control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        assert _call(srv, "GET", "/api/v1/contours", CONTROL_KEY)[0] == 200
        assert _call(srv, "GET", "/api/v1/market-data/source", CONTROL_KEY)[0] == 401
    finally:
        srv.close()


def test_server_needs_some_key(tmp_path):
    with pytest.raises(ValueError):
        MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), None)


def test_swagger_declares_control_routes():
    post = SPEC["paths"]["/api/v1/contours"]["post"]
    assert post["security"] == [{"ControlToken": []}]
    assert set(post["responses"]) >= {"201", "202", "400", "401", "403", "409", "422"}
