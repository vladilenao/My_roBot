import json
import threading
from urllib import error, request

import pytest

from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore
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
            control.mark_launched(SOURCE_API, [NG], "--mode emulation --instruments NG-12.26", pending)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class TestLaunchControl:
    def test_command_is_answered_by_main_thread(self):
        control = LaunchControl()
        control.accept_commands()
        _main_thread(control, "run")
        status, payload = control.submit(parse_command({"kind": "emulation", "instruments": ["NG"]}))
        assert status == 201 and payload["state"] == "starting" and payload["source"] == "api"
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
        control.mark_launched(SOURCE_CLI, [NG], "--mode emulation --instruments NG-12.26")
        status, payload = control.submit(parse_command({"kind": "emulation"}))
        assert status == 409 and payload["contour"]["source"] == "cli"


class TestContourProcessState:
    """Состояние процесса контура (add-contour-process): starting → running → stopped/failed."""

    def test_running_after_first_checkpoint(self):
        control = LaunchControl()
        control.mark_launched(SOURCE_CLI, [NG], "--mode emulation --instruments NG-12.26")
        control.mark_contour_running()
        assert control.snapshot()["state"] == "starting"
        control.mark_process(4242)
        control.mark_contour_running()
        snapshot = control.snapshot()
        assert (snapshot["state"], snapshot["pid"], snapshot["exit_code"]) == ("running", 4242, None)

    def test_failed_keeps_exit_code(self):
        control = LaunchControl()
        control.mark_launched(SOURCE_API, [NG], "--mode emulation --instruments NG-12.26")
        control.mark_process(4242)
        control.mark_exited(1)
        snapshot = control.snapshot()
        assert (snapshot["state"], snapshot["pid"], snapshot["exit_code"]) == ("failed", None, 1)

    def test_stopped_on_clean_exit(self):
        control = LaunchControl()
        control.mark_process(4242)
        control.mark_exited(0, stopping=True)
        assert control.snapshot()["state"] == "stopped"


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


# ── Остановка, повторный запуск, лог (add-control-api) ──

def _controlled(tmp_path, *, alive=True):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    control = LaunchControl(reply_timeout=2, core_state=store, contour_dir=tmp_path / "contours" / "emulation")
    control.mark_launched(SOURCE_API, [NG], "--mode emulation --instruments NG-12.26")
    store.record_launch("emulation", "emulation", [NG.payload()], source="api")
    if alive:
        control.mark_process(4242)
    return control, store


class TestStopAndRelaunch:
    def test_stop_records_generation_and_shows_stopping(self, tmp_path):
        control, store = _controlled(tmp_path)
        status, payload = control.stop("emulation", "no_entries")
        assert status == 202 and payload["generation"] == 2
        assert payload["contour"]["state"] == "stopping_no_entries"
        assert payload["contour"]["desired"] == {"running": False, "stop_mode": "no_entries", "generation": 2}

    def test_repeat_and_mode_rules(self, tmp_path):
        control, _ = _controlled(tmp_path)
        control.stop("emulation", "no_entries")
        assert control.stop("emulation", "no_entries")[0] == 200
        assert control.stop("emulation", "close_all")[1]["generation"] == 3
        status, payload = control.stop("emulation", "no_entries")
        assert status == 409 and payload["code"] == "MODE_CONFLICT"

    def test_nothing_to_stop_and_unknown_contour(self, tmp_path):
        control, store = _controlled(tmp_path, alive=False)
        status, payload = control.stop("emulation", "close_all")
        assert status == 409 and payload["code"] == "NOT_RUNNING"
        assert store.desired("emulation").running and store.commands()[0]["outcome"] == "conflict"
        assert control.stop("account", "close_all")[0] == 404

    def test_launch_while_draining_is_stopping_conflict(self, tmp_path):
        control, _ = _controlled(tmp_path)
        control.mark_contour_running()
        control.stop("emulation", "no_entries")
        control.accept_commands()
        status, payload = control.submit(parse_command({"kind": "emulation"}))
        assert status == 409 and payload["code"] == "STOPPING"

    def test_relaunch_after_stopped_is_accepted(self, tmp_path):
        control, _ = _controlled(tmp_path)
        control.stop("emulation", "no_entries")
        control.mark_exited(0)
        control.accept_commands()
        assert control.snapshot()["state"] == "stopped"
        _main_thread(control, "run")
        status, payload = control.submit(parse_command({"kind": "emulation", "instruments": ["NG"]}))
        assert status == 201 and payload["state"] == "starting"

    def test_core_restart_shows_previous_desired_and_waits(self, tmp_path):
        CoreStateStore(tmp_path / "core.sqlite3").record_launch("emulation", "emulation", [NG.payload()])
        directory = tmp_path / "contours" / "emulation"
        directory.mkdir(parents=True)
        write_status(directory / "status.json", {"state": "running", "applied_generation": 1})
        control = LaunchControl(core_state=CoreStateStore(tmp_path / "core.sqlite3"), contour_dir=directory)
        control.accept_commands()
        snapshot = control.snapshot()
        assert snapshot["state"] == "waiting_command" and snapshot["pid"] is None
        assert snapshot["desired"] == {"running": True, "stop_mode": None, "generation": 1}
        # Снимок прошлого запуска ядра не выдаётся за фактическое.
        assert snapshot["actual"] is None

    def test_actual_comes_from_status_file(self, tmp_path):
        control, _ = _controlled(tmp_path)
        directory = tmp_path / "contours" / "emulation"
        directory.mkdir(parents=True)
        write_status(directory / "status.json", {"state": "running", "applied_generation": 1, "open_positions": 2,
                                                 "pending_entries": 0, "last_tick_at": "2026-10-24T10:00:00Z",
                                                 "pid": 4242, "version": "7.1.0"})
        assert control.snapshot()["actual"] == {"state": "running", "applied_generation": 1, "open_positions": 2,
                                               "pending_entries": 0, "last_tick_at": "2026-10-24T10:00:00Z"}
        (directory / "status.json").write_text("{broken", encoding="utf-8")
        assert control.snapshot()["actual"] is None


@pytest.fixture
def controlled_server(tmp_path):
    control, store = _controlled(tmp_path)
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), EXPORT_KEY, port=0,
                                 control=control, control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, control, tmp_path
    srv.close()


def _text(srv, path, key):
    address, port = srv.address
    req = request.Request(f"http://{address}:{port}{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


class TestStopAndLogApi:
    def test_stop_matches_contract(self, controlled_server):
        srv, _, _ = controlled_server
        status, payload, headers = _call(srv, "POST", "/api/v1/contours/emulation/stop", CONTROL_KEY,
                                         {"mode": "no_entries"})
        assert status == 202 and headers["Location"] == "/api/v1/contours/emulation"
        _check(payload, {"$ref": "#/components/schemas/ContourStopAccepted"})
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/stop", CONTROL_KEY, {"mode": "no_entries"})
        assert status == 200 and payload["generation"] == 2
        status, payload, _ = _call(srv, "GET", "/api/v1/contours/emulation", CONTROL_KEY)
        assert status == 200 and payload["state"] == "stopping_no_entries"
        _check(payload, {"$ref": "#/components/schemas/Contour"})

    @pytest.mark.parametrize("body", [{"mode": "pause"}, {}, {"mode": "close_all", "extra": 1}, b"{bad"])
    def test_bad_stop_body(self, controlled_server, body):
        srv, _, _ = controlled_server
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/stop", CONTROL_KEY, body)
        assert status == 400 and payload["code"] == "INVALID_REQUEST"

    def test_unknown_contour(self, controlled_server):
        srv, _, _ = controlled_server
        assert _call(srv, "GET", "/api/v1/contours/account", CONTROL_KEY)[0] == 404
        assert _call(srv, "POST", "/api/v1/contours/account/stop", CONTROL_KEY, {"mode": "close_all"})[0] == 404
        assert _text(srv, "/api/v1/contours/account/log", CONTROL_KEY)[0] == 404

    def test_stop_conflict_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server
        control.mark_exited(0)
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/stop", CONTROL_KEY, {"mode": "close_all"})
        assert status == 409 and payload["code"] == "NOT_RUNNING"
        _check(payload, {"$ref": "#/components/schemas/ContourConflict"})

    def test_log_tail_and_limits(self, controlled_server):
        srv, _, tmp_path = controlled_server
        directory = tmp_path / "contours" / "emulation"
        assert _text(srv, "/api/v1/contours/emulation/log", CONTROL_KEY)[:2] == (200, "")
        directory.mkdir(parents=True)
        lines = [f"строка {number} токен [скрыто]\n" for number in range(5000)]
        (directory / "bot_debug.log").write_text("".join(lines), encoding="utf-8")
        status, text, headers = _text(srv, "/api/v1/contours/emulation/log?tail=3", CONTROL_KEY)
        assert status == 200 and text == "".join(lines[-3:]) and headers["Content-Type"].startswith("text/plain")
        assert len(_text(srv, "/api/v1/contours/emulation/log", CONTROL_KEY)[1].splitlines()) == 200
        for tail in ("0", "5000", "abc"):
            assert _text(srv, f"/api/v1/contours/emulation/log?tail={tail}", CONTROL_KEY)[0] == 400
        assert CONTROL_KEY not in _text(srv, "/api/v1/contours/emulation/log?tail=1000", CONTROL_KEY)[1]

    def test_new_routes_require_control_key_and_loopback(self, controlled_server):
        srv, _, _ = controlled_server
        assert _call(srv, "POST", "/api/v1/contours/emulation/stop", EXPORT_KEY, {"mode": "close_all"})[0] == 401
        assert _text(srv, "/api/v1/contours/emulation/log", EXPORT_KEY)[0] == 401
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/stop", CONTROL_KEY, {"mode": "close_all"},
                                   host=f"evil.example:{srv.address[1]}")
        assert status == 403 and payload["code"] == "FORBIDDEN_HOST"


class TestMembershipApi:
    def test_add_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server

        def main():
            command = control.next_membership(2)
            control.answer_membership(command, 202, {"generation": 2, "contour": control.snapshot()})
        threading.Thread(target=main, daemon=True).start()
        status, payload, headers = _call(srv, "POST", "/api/v1/contours/emulation/instruments", CONTROL_KEY,
                                         {"add": ["NG"]})
        assert status == 202 and headers["Location"] == "/api/v1/contours/emulation"
        _check(payload, {"$ref": "#/components/schemas/ContourMembershipAccepted"})
        _check(payload["contour"], {"$ref": "#/components/schemas/Contour"})

    @pytest.mark.parametrize("body", [{}, {"add": []}, {"add": ["NG"], "remove": ["SBER"]}, b"{bad"])
    def test_bad_body(self, controlled_server, body):
        srv, _, _ = controlled_server
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/instruments", CONTROL_KEY, body)
        assert status == 400 and payload["code"] == "INVALID_REQUEST"

    def test_unknown_contour_and_keys(self, controlled_server):
        srv, _, _ = controlled_server
        assert _call(srv, "POST", "/api/v1/contours/account/instruments", CONTROL_KEY, {"add": ["NG"]})[0] == 404
        assert _call(srv, "POST", "/api/v1/contours/emulation/instruments", EXPORT_KEY, {"add": ["NG"]})[0] == 401

    def test_not_running_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server
        control.mark_exited(0)
        status, payload, _ = _call(srv, "POST", "/api/v1/contours/emulation/instruments", CONTROL_KEY,
                                   {"remove": ["NG"]})
        assert status == 409 and payload["code"] == "NOT_RUNNING"
        _check(payload, {"$ref": "#/components/schemas/ContourConflict"})
