"""API управления 2.0 (rework-contour-api): эмуляция, история, точка входа, ошибки, действия, ревизия."""

import json
import threading
from urllib import error, request

import pytest

from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore
from src.bot.launch import Chosen, Rejection, SOURCE_API, SOURCE_CLI
from src.bot.launch_control import BadCommand, LaunchControl, parse_add, parse_history, parse_target
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from src.history.report import MarketDataSync
from tests.unit.clusters.test_report import SPEC, _check

CONTROL_KEY = "control-test-key"
EXPORT_KEY = "export-test-key"
NG = Chosen("NG-12.26", "NG-12.26", "future", 52, "NG")
NG_TUPLE = ("NG (Природный газ) — NG-12.26", "NGZ6", "future", "NG-12.26")
COMMAND = '{"mode": "on", "instruments": ["NG-12.26"]}'
INTERNAL_WORDS = {"desired", "actual", "generation", "applied_generation", "running", "stop_mode"}


def _keys(value) -> set:
    """Все ключи объектов ответа на любой глубине."""
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value)) if value else set()
    return set()


def _start(body=None, expected=None):
    return parse_target({"mode": "on", **(body or {})}, expected)


class TestParse:
    def test_start_defaults_to_all(self):
        mode, launch = _start()
        assert mode == "on" and launch.instruments == ("ALL",) and launch.source == SOURCE_API

    def test_stop_modes_have_no_launch(self):
        assert parse_target({"mode": "no_entries"}) == ("no_entries", None)
        assert parse_target({"mode": "close_all"}) == ("close_all", None)

    def test_if_match_travels_with_launch(self):
        assert _start({"instruments": ["ng-12.26", "SBER"]}, expected=4)[1].expected_revision == 4

    @pytest.mark.parametrize("body", [
        [], {}, {"kind": "emulation"}, {"mode": "pause"}, {"mode": "on", "extra": 1},
        {"mode": "no_entries", "instruments": ["SBER"]}, {"mode": "on", "instruments": []},
        {"mode": "on", "instruments": ["NG-12.26", 5]}, {"mode": "on", "instruments": ["all", "SBER"]},
    ])
    def test_bad_target_bodies(self, body):
        with pytest.raises(BadCommand) as exc:
            parse_target(body)
        assert exc.value.code == "INVALID_REQUEST"

    @pytest.mark.parametrize("body", [
        [], {}, {"from": "2026-09-08 10:00", "to": "2026-09-01 10:00"},
        {"from": "2026-09-01", "to": "2026-09-02", "pause": -1}, {"from": "2026-09-01", "to": "2026-09-02", "pause": True},
        {"from": "01.09.2026", "to": "2026-09-02"}, {"from": "2026-09-01", "to": "2026-09-02", "kind": "history"},
    ])
    def test_bad_history_bodies(self, body):
        with pytest.raises(BadCommand):
            parse_history(body)


def _main_thread(control, outcome, instruments=None):
    """Имитация главного потока робота: принять одну команду и ответить."""
    def run():
        pending = control.next_command(poll_secs=0.05)
        if outcome == "reject":
            control.reject(pending, [Rejection("AFLT", "У инструмента нет привязок в robot.toml.")])
        else:
            control.mark_launched(SOURCE_API, [NG], COMMAND, pending, instruments=instruments)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class TestLaunchControl:
    def test_command_is_answered_by_main_thread(self):
        control = LaunchControl()
        control.accept_commands()
        _main_thread(control, "run")
        status, payload = control.put_target(*_start({"instruments": ["NG-12.26"]}))
        assert status == 202 and payload["state"] == "starting" and payload["launch"]["source"] == "api"
        assert payload["launch"]["request"] == {"mode": "on", "instruments": ["NG-12.26"]}
        assert control.put_target(*_start())[0] == 409

    def test_rejection_keeps_waiting(self):
        control = LaunchControl()
        control.accept_commands()
        _main_thread(control, "reject")
        status, payload = control.put_target(*_start({"instruments": ["AFLT"]}))
        assert status == 422 and payload["rejections"][0]["token"] == "AFLT"
        view = control.emulation()
        assert view["state"] == "waiting_command"
        assert view["launch"]["rejections"] == [{"instrument": "AFLT", "reason": "У инструмента нет привязок в robot.toml."}]

    def test_slow_resolution_answers_202(self):
        control = LaunchControl(reply_timeout=0.05)
        control.accept_commands()
        status, payload = control.put_target(*_start())
        assert status == 202 and payload["state"] == "starting"

    def test_not_waiting_means_conflict(self):
        control = LaunchControl()
        control.mark_launched(SOURCE_CLI, [NG], COMMAND)
        status, payload = control.put_target(*_start())
        assert status == 409 and payload["code"] == "ALREADY_RUNNING"

    def test_launch_is_recorded_at_command_time(self, tmp_path):
        store = CoreStateStore(tmp_path / "core.sqlite3")
        control = LaunchControl(core_state=store)
        control.accept_commands()
        _main_thread(control, "run", instruments=[NG_TUPLE])
        status, payload = control.put_target(*_start({"instruments": ["NG-12.26"]}))
        assert status == 202 and payload["target"] == {"mode": "on", "revision": 1}
        assert [member.name for member in store.desired("emulation").members] == ["NG-12.26"]
        assert control.launch_recorded()
        control.mark_process(4242)
        assert not control.launch_recorded()

    def test_stale_if_match_at_record_time_is_412(self, tmp_path):
        store = CoreStateStore(tmp_path / "core.sqlite3")
        store.record_launch("emulation", "emulation", [], source="api")
        store.record_stop("emulation", "no_entries")
        control = LaunchControl(core_state=store)
        control.accept_commands()
        pending = None

        def main():
            nonlocal pending
            pending = control.next_command(poll_secs=0.05)
            store.record_stop("emulation", "close_all")      # другой клиент успел раньше: ревизия 3
            accepted.append(control.mark_launched(SOURCE_API, [NG], COMMAND, pending, instruments=[NG_TUPLE]))
        accepted = []
        threading.Thread(target=main, daemon=True).start()
        status, payload = control.put_target(*_start(expected=2))
        assert status == 412 and payload["code"] == "REVISION_CHANGED" and accepted == [False]
        assert store.desired("emulation").generation == 3 and control.emulation()["state"] == "waiting_command"
        assert store.commands()[0]["outcome"] == "conflict"


class TestContourProcessState:
    """Состояние процесса контура (add-contour-process): starting → running → stopped/failed."""

    def test_running_after_first_checkpoint(self):
        control = LaunchControl()
        control.mark_launched(SOURCE_CLI, [NG], COMMAND)
        control.mark_contour_running()
        assert control.emulation()["state"] == "starting"
        control.mark_process(4242)
        control.mark_contour_running()
        view = control.emulation()
        assert view["state"] == "running"
        assert view["process"] == {"pid": 4242, "exit_code": None, "exited_at": None, "dir": "contours/emulation"}

    def test_failed_keeps_exit_code(self):
        control = LaunchControl()
        control.mark_launched(SOURCE_API, [NG], COMMAND)
        control.mark_process(4242)
        control.mark_exited(1)
        view = control.emulation()
        assert (view["state"], view["process"]["pid"], view["process"]["exit_code"]) == ("failed", None, 1)
        assert view["process"]["exited_at"].endswith("Z")

    def test_stopped_on_clean_exit(self):
        control = LaunchControl()
        control.mark_process(4242)
        control.mark_exited(0, stopping=True)
        assert control.emulation()["state"] == "stopped"


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


def _call(srv, method, path, key, body=None, host=None, if_match=None):
    address, port = srv.address
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if host:
        headers["Host"] = host
    if if_match is not None:
        headers["If-Match"] = if_match
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = request.Request(f"http://{address}:{port}{path}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


def _problem(status, payload, headers, code):
    """Ошибка управления — problem+json с этим кодом и схемой Problem."""
    assert headers["Content-Type"].startswith("application/problem+json"), headers["Content-Type"]
    assert payload["code"] == code and payload["status"] == status
    assert payload["type"] == "urn:my-robot:problem:" + code.lower().replace("_", "-")
    _check(payload, {"$ref": "#/components/schemas/Problem"})


class TestEmulationApi:
    def test_waiting_state_matches_contract(self, server):
        srv, control = server
        control.accept_commands()
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation", CONTROL_KEY)
        assert status == 200 and payload["state"] == "waiting_command" and "ETag" not in headers
        _check(payload, {"$ref": "#/components/schemas/Emulation"})
        assert payload["_actions"] == [{"name": "start", "title": "Запустить эмуляцию", "method": "PUT",
                                        "href": "/api/v1/emulation/target", "body": {"mode": "on"}}]
        assert not any(name.lower().startswith("access-control") for name in headers)

    def test_start_then_conflict(self, server):
        srv, control = server
        control.accept_commands()
        _main_thread(control, "run")
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY,
                                         {"mode": "on", "instruments": ["NG-12.26"]})
        assert status == 202 and headers["Location"] == "/api/v1/emulation"
        _check(payload, {"$ref": "#/components/schemas/Emulation"})
        assert payload["instruments"][0]["name"] == "NG-12.26" and payload["instruments"][0]["days_to_expiry"] == 52
        assert not _keys(payload) & INTERNAL_WORDS
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "on"})
        _problem(status, payload, headers, "ALREADY_RUNNING")

    def test_rejected_command(self, server):
        srv, control = server
        control.accept_commands()
        _main_thread(control, "reject")
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY,
                                         {"mode": "on", "instruments": ["AFLT"]})
        assert status == 422
        _problem(status, payload, headers, "INSTRUMENTS_REJECTED")
        assert payload["rejections"] == [{"instrument": "AFLT", "reason": "У инструмента нет привязок в robot.toml."}]

    def test_slow_command_points_to_state(self, server):
        srv, control = server
        control._reply_timeout = 0.05
        control.accept_commands()
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "on"})
        assert status == 202 and headers["Location"] == "/api/v1/emulation" and payload["state"] == "starting"

    @pytest.mark.parametrize("body", [b"{bad", {"kind": "emulation"}, {"mode": "no_entries", "instruments": ["SBER"]}])
    def test_bad_body(self, server, body):
        srv, control = server
        control.accept_commands()
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, body)
        assert status == 400
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_target_is_404_before_first_launch(self, server):
        srv, _ = server
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation/target", CONTROL_KEY)
        _problem(status, payload, headers, "NOT_FOUND")

    def test_export_key_cannot_control_and_control_key_cannot_export(self, server):
        srv, _ = server
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation", EXPORT_KEY)
        assert status == 401
        _problem(status, payload, headers, "UNAUTHORIZED")
        assert _call(srv, "PUT", "/api/v1/emulation/target", EXPORT_KEY, {"mode": "on"})[0] == 401
        status, payload, headers = _call(srv, "GET", "/api/v1/market-data/source", CONTROL_KEY)
        # Ошибка экспорта — прежний формат {code, message}: его читает эмулятор данных.
        assert status == 401 and payload == {"code": "UNAUTHORIZED", "message": "Токен отсутствует или неверен."}
        assert headers["Content-Type"].startswith("application/json")

    def test_foreign_host_forbidden(self, server):
        srv, _ = server
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "on"},
                                         host=f"evil.example:{srv.address[1]}")
        _problem(status, payload, headers, "FORBIDDEN_HOST")

    def test_key_never_returned(self, server):
        srv, control = server
        control.accept_commands()
        for path in ("/api/v1", "/api/v1/emulation", "/openapi.yaml"):
            address, port = srv.address
            req = request.Request(f"http://{address}:{port}{path}", headers={"Authorization": f"Bearer {CONTROL_KEY}"})
            with request.urlopen(req, timeout=5) as response:
                assert CONTROL_KEY not in response.read().decode()


class TestRemovedRoutes:
    @pytest.mark.parametrize("method, path, body", [
        ("GET", "/api/v1/contours", None), ("POST", "/api/v1/contours", {"kind": "emulation"}),
        ("GET", "/api/v1/contours/emulation", None), ("POST", "/api/v1/contours/emulation/stop", {"mode": "close_all"}),
        ("POST", "/api/v1/contours/emulation/instruments", {"add": ["NG"]}),
        ("POST", "/api/v1/history/20261024-101500/instruments", {"instruments": ["SBER"]}),
    ])
    def test_old_routes_are_404_and_do_nothing(self, server, method, path, body):
        srv, control = server
        control.accept_commands()
        status, payload, headers = _call(srv, method, path, CONTROL_KEY, body)
        _problem(status, payload, headers, "NOT_FOUND")
        assert control.emulation()["state"] == "waiting_command"


class TestHome:
    def test_control_key_sees_contours(self, server):
        srv, control = server
        control.accept_commands()
        status, payload, _ = _call(srv, "GET", "/api/v1", CONTROL_KEY)
        assert status == 200
        _check(payload, {"$ref": "#/components/schemas/Home"})
        assert payload["emulation"] == {"state": "waiting_command", "instruments": 0, "open_positions": None}
        assert payload["history"] == {"max": 0, "running": 0, "queued": 0} and payload["live"] is None
        assert payload["sandbox"] is None
        assert set(payload["_links"]) == {"self", "core", "emulation", "history", "docs"}

    def test_export_key_sees_export_only(self, server):
        srv, _ = server
        status, payload, _ = _call(srv, "GET", "/api/v1", EXPORT_KEY)
        assert status == 200 and payload["emulation"] is None and payload["history"] is None
        assert set(payload["_links"]) == {"self", "market-data", "clusters-report", "docs"}

    def test_no_key(self, server):
        srv, _ = server
        status, payload, headers = _call(srv, "GET", "/api/v1", None)
        _problem(status, payload, headers, "UNAUTHORIZED")

    def test_core_not_ready_is_shown(self, server):
        srv, control = server
        srv.core_state = lambda: {"state": "retrying"}
        assert _call(srv, "GET", "/api/v1", CONTROL_KEY)[1]["core"] == {"state": "retrying"}


def test_server_with_only_control_key(tmp_path):
    control = LaunchControl()
    srv = MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), None, port=0,
                                 control=control, control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        assert _call(srv, "GET", "/api/v1/emulation", CONTROL_KEY)[0] == 200
        assert _call(srv, "GET", "/api/v1/market-data/source", CONTROL_KEY)[0] == 401
    finally:
        srv.close()


def test_server_needs_some_key(tmp_path):
    with pytest.raises(ValueError):
        MarketDataExportServer(MarketDataStore(tmp_path / "market.sqlite3"), None)


class TestContract:
    def test_version_and_control_routes(self):
        assert SPEC["info"]["version"] == "2.2.0"
        put = SPEC["paths"]["/api/v1/emulation/target"]["put"]
        assert put["security"] == [{"ControlToken": []}]
        assert set(put["responses"]) >= {"200", "202", "400", "401", "403", "409", "412", "422"}
        assert not any(path.startswith("/api/v1/contours") for path in SPEC["paths"])
        assert "/api/v1/live" not in SPEC["paths"] and "/api/v1/instruments" not in SPEC["paths"]

    def test_every_operation_has_a_handler(self, history_server):
        """Ни одной операции контракта, на которую сервер ответил бы «Эндпоинт не найден»."""
        srv, _, _ = history_server
        samples = {"{name}": "NG-12.26", "{run_id}": "20261024-101500", "{consumer_id}": "emulator"}
        for path, item in SPEC["paths"].items():
            for method in ("get", "put", "post"):
                if method not in item or path in ("/openapi.yaml", "/docs"):
                    continue
                concrete = path
                for name, value in samples.items():
                    concrete = concrete.replace(name, value)
                key = EXPORT_KEY if "ExportToken" in json.dumps(item[method].get("security", SPEC["security"])) \
                    and "ControlToken" not in json.dumps(item[method].get("security", [])) else CONTROL_KEY
                address, port = srv.address
                req = request.Request(f"http://{address}:{port}{concrete}", method=method.upper(),
                                      data=None if method == "get" else b"{}",
                                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                try:
                    with request.urlopen(req, timeout=5) as response:
                        text = response.read().decode("utf-8")
                except error.HTTPError as exc:
                    text = exc.read().decode("utf-8")
                assert "Эндпоинт не найден" not in text, (method, path)

    def test_openapi_route_serves_2_1_0(self, server):
        srv, _ = server
        address, port = srv.address
        with request.urlopen(f"http://{address}:{port}/openapi.yaml", timeout=5) as response:
            assert "version: 2.2.0" in response.read().decode("utf-8")


# ── Остановка, повторный запуск, журнал, состав ──

def _controlled(tmp_path, *, alive=True):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    control = LaunchControl(reply_timeout=2, core_state=store, contour_dir=tmp_path / "contours" / "emulation")
    control.mark_launched(SOURCE_API, [NG], COMMAND)
    store.record_launch("emulation", "emulation", [NG.payload()], source="api", command=COMMAND)
    if alive:
        control.mark_process(4242)
    return control, store


class TestStopAndRelaunch:
    def test_stop_records_revision_and_shows_stopping(self, tmp_path):
        control, _ = _controlled(tmp_path)
        status, payload = control.put_target("no_entries", None)
        assert status == 202 and payload["target"] == {"mode": "no_entries", "revision": 2}
        assert payload["state"] == "stopping_no_entries"

    def test_repeat_and_mode_rules(self, tmp_path):
        control, store = _controlled(tmp_path)
        control.put_target("no_entries", None)
        assert control.put_target("no_entries", None)[0] == 200
        assert store.commands()[0]["outcome"] == "ignored"
        assert control.put_target("close_all", None)[1]["target"]["revision"] == 3
        status, payload = control.put_target("no_entries", None)
        assert status == 409 and payload["code"] == "MODE_CONFLICT"

    def test_nothing_to_stop(self, tmp_path):
        control, store = _controlled(tmp_path, alive=False)
        status, payload = control.put_target("close_all", None)
        assert status == 409 and payload["code"] == "NOT_RUNNING"
        assert store.desired("emulation").running and store.commands()[0]["outcome"] == "conflict"

    def test_launch_while_draining_is_stopping_conflict(self, tmp_path):
        control, _ = _controlled(tmp_path)
        control.mark_contour_running()
        control.put_target("no_entries", None)
        control.accept_commands()
        status, payload = control.put_target(*_start())
        assert status == 409 and payload["code"] == "STOPPING"

    def test_relaunch_after_stopped_is_accepted(self, tmp_path):
        control, _ = _controlled(tmp_path)
        control.put_target("no_entries", None)
        control.mark_exited(0)
        control.accept_commands()
        assert control.emulation()["state"] == "stopped"
        _main_thread(control, "run")
        status, payload = control.put_target(*_start({"instruments": ["NG-12.26"]}))
        assert status == 202 and payload["state"] == "starting"

    def test_core_restart_shows_previous_target_and_waits(self, tmp_path):
        CoreStateStore(tmp_path / "core.sqlite3").record_launch("emulation", "emulation", [NG.payload()],
                                                                 source="api", command=COMMAND)
        directory = tmp_path / "contours" / "emulation"
        directory.mkdir(parents=True)
        write_status(directory / "status.json", {"state": "running", "applied_generation": 1})
        control = LaunchControl(core_state=CoreStateStore(tmp_path / "core.sqlite3"), contour_dir=directory)
        control.accept_commands()
        view = control.emulation()
        assert view["state"] == "waiting_command" and view["process"] is None
        assert view["target"] == {"mode": "on", "revision": 1}
        assert view["launch"]["source"] == "api" and view["launch"]["request"]["instruments"] == ["NG-12.26"]
        # Снимок прошлого запуска ядра не выдаётся за текущее состояние.
        assert view["current"] is None

    def test_current_comes_from_status_file_and_lags_target(self, tmp_path):
        control, _ = _controlled(tmp_path)
        directory = tmp_path / "contours" / "emulation"
        directory.mkdir(parents=True)
        write_status(directory / "status.json", {"state": "running", "applied_generation": 1, "open_positions": 2,
                                                 "pending_entries": 0, "last_tick_at": "2026-10-24T10:00:00Z",
                                                 "pid": 4242, "version": "7.1.0", "core_link": "lost",
                                                 "members": [{"key": "NG-12.26", "state": "trading"}]})
        control.put_target("no_entries", None)
        view = control.emulation()
        assert view["current"] == {"state": "running", "applied_revision": 1, "open_positions": 2,
                                   "pending_entries": 0, "last_tick_at": "2026-10-24T10:00:00Z", "core_link": "lost"}
        assert view["target"]["revision"] == 2 and view["state"] == "stopping_no_entries"
        (directory / "status.json").write_text("{broken", encoding="utf-8")
        assert control.emulation()["current"] is None

    def test_old_command_string_has_no_request(self, tmp_path):
        store = CoreStateStore(tmp_path / "core.sqlite3")
        store.record_launch("emulation", "emulation", [], source="cli", command="--mode emulation --instruments NG")
        view = LaunchControl(core_state=store).emulation()
        assert view["launch"]["source"] == "cli" and view["launch"]["request"] is None
        _check(view, {"$ref": "#/components/schemas/Emulation"})


class TestActionsMatchCommands:
    """Каждое действие из ``_actions`` сервер принимает, остальные команды режима — отклоняет."""

    STOPS = {"stop": "no_entries", "stop-close-all": "close_all"}

    def _names(self, control):
        return [action["name"] for action in control.emulation()["_actions"]]

    def test_running(self, tmp_path):
        control, _ = _controlled(tmp_path)
        assert self._names(control) == ["stop", "stop-close-all", "add-instruments"]
        assert control.put_target(*_start())[0] == 409

    @pytest.mark.parametrize("first, names", [("no_entries", ["stop-close-all"]), ("close_all", [])])
    def test_stopping(self, tmp_path, first, names):
        control, _ = _controlled(tmp_path)
        control.put_target(first, None)
        assert self._names(control) == names
        for name in names:
            assert control.put_target(self.STOPS[name], None)[0] == 202
        if first == "close_all":
            assert control.put_target("no_entries", None)[0] == 409
        assert control.change_membership(parse_add({"instruments": ["SBER"]}))[1]["code"] == "STOPPING"

    def test_waiting_only_start(self, tmp_path):
        control, _ = _controlled(tmp_path, alive=False)
        control.mark_exited(0)
        assert self._names(control) == []                  # главный поток ещё не ждёт команду
        control.accept_commands()
        assert self._names(control) == ["start"]
        assert control.put_target("no_entries", None)[0] == 409
        control.core_status = lambda: {"state": "retrying"}
        assert self._names(control) == []
        assert control.put_target(*_start())[1]["code"] == "CORE_NOT_READY"

    def test_member_actions(self, tmp_path):
        control, store = _controlled(tmp_path)
        [member] = control.emulation()["instruments"]
        assert [action["name"] for action in member["_actions"]] == ["remove", "remove-close-all"]
        assert member["_actions"][0]["body"] == {"instruments": ["NG-12.26"], "mode": "no_entries"}
        key = store.desired("emulation").members[0].key
        store.record_membership("emulation", remove=[key])
        [member] = control.emulation()["instruments"]
        assert (member["target"], [action["name"] for action in member["_actions"]]) == (
            "removed", ["remove-close-all", "restore"])
        store.record_membership("emulation", remove=[key], mode="close_all")
        assert [action["name"] for action in control.emulation()["instruments"][0]["_actions"]] == ["restore"]
        control.put_target("no_entries", None)
        assert control.emulation()["instruments"][0]["_actions"] == []


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
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"})
        assert status == 202 and headers["Location"] == "/api/v1/emulation" and headers["ETag"] == '"2"'
        _check(payload, {"$ref": "#/components/schemas/Emulation"})
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"})
        assert status == 200 and payload["target"]["revision"] == 2 and headers["ETag"] == '"2"'
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation", CONTROL_KEY)
        assert status == 200 and payload["state"] == "stopping_no_entries" and headers["ETag"] == '"2"'
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation/target", CONTROL_KEY)
        assert (status, payload, headers["ETag"]) == (200, {"mode": "no_entries", "revision": 2}, '"2"')

    @pytest.mark.parametrize("body", [{"mode": "pause"}, {}, {"mode": "close_all", "extra": 1}, b"{bad"])
    def test_bad_stop_body(self, controlled_server, body):
        srv, _, _ = controlled_server
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, body)
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_stop_conflict_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server
        control.mark_exited(0)
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "close_all"})
        _problem(status, payload, headers, "NOT_RUNNING")

    def test_log_tail_and_limits(self, controlled_server):
        srv, _, tmp_path = controlled_server
        directory = tmp_path / "contours" / "emulation"
        assert _text(srv, "/api/v1/emulation/log", CONTROL_KEY)[:2] == (200, "")
        directory.mkdir(parents=True)
        lines = [f"строка {number} токен [скрыто]\n" for number in range(5000)]
        (directory / "bot_debug.log").write_text("".join(lines), encoding="utf-8")
        status, text, headers = _text(srv, "/api/v1/emulation/log?tail=3", CONTROL_KEY)
        assert status == 200 and text == "".join(lines[-3:]) and headers["Content-Type"].startswith("text/plain")
        assert len(_text(srv, "/api/v1/emulation/log", CONTROL_KEY)[1].splitlines()) == 200
        for tail in ("0", "5000", "abc"):
            assert _text(srv, f"/api/v1/emulation/log?tail={tail}", CONTROL_KEY)[0] == 400
        assert CONTROL_KEY not in _text(srv, "/api/v1/emulation/log?tail=1000", CONTROL_KEY)[1]

    def test_routes_require_control_key_and_loopback(self, controlled_server):
        srv, _, _ = controlled_server
        assert _call(srv, "PUT", "/api/v1/emulation/target", EXPORT_KEY, {"mode": "close_all"})[0] == 401
        assert _text(srv, "/api/v1/emulation/log", EXPORT_KEY)[0] == 401
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "close_all"},
                                         host=f"evil.example:{srv.address[1]}")
        _problem(status, payload, headers, "FORBIDDEN_HOST")


class TestIfMatch:
    def test_stale_revision_is_412_and_changes_nothing(self, controlled_server):
        srv, control, _ = controlled_server
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"},
                                         if_match='"7"')
        _problem(status, payload, headers, "REVISION_CHANGED")
        assert control.target() == {"mode": "on", "revision": 1}

    def test_two_commands_with_one_revision(self, controlled_server):
        srv, control, _ = controlled_server
        first = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"}, if_match='"1"')
        second = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "close_all"}, if_match='"1"')
        assert (first[0], second[0]) == (202, 412) and control.target()["mode"] == "no_entries"

    @pytest.mark.parametrize("value", ["*", 'W/"1"', "1"])
    def test_any_and_weak_and_bare_forms(self, controlled_server, value):
        srv, _, _ = controlled_server
        assert _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"}, if_match=value)[0] == 202

    def test_garbage_never_matches(self, controlled_server):
        srv, _, _ = controlled_server
        assert _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "no_entries"},
                     if_match="abc")[0] == 412


class TestMembershipApi:
    def test_add_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server

        def main():
            command = control.next_membership(2)
            control.answer_membership(command, 202, {"revision": 2})
        threading.Thread(target=main, daemon=True).start()
        status, payload, headers = _call(srv, "POST", "/api/v1/emulation/instruments", CONTROL_KEY,
                                         {"instruments": ["SBER"]})
        assert status == 202 and payload["revision"] == 2 and headers["ETag"] == '"2"'
        _check(payload, {"$ref": "#/components/schemas/MemberList"})

    def test_remove_answer_and_rejection(self, controlled_server):
        srv, control, _ = controlled_server

        def main():
            command = control.next_membership(2)
            assert (command.remove, command.mode) == (("NG-12.26", "SBER"), "close_all")
            control.answer_membership(command, 422, {"code": "INSTRUMENTS_REJECTED", "message": "Состав не изменён.",
                                                     "rejections": [{"token": "SBER", "reason": "нет в составе"}]})
        threading.Thread(target=main, daemon=True).start()
        status, payload, headers = _call(srv, "POST", "/api/v1/emulation/instruments/remove", CONTROL_KEY,
                                         {"instruments": ["NG-12.26", "SBER"], "mode": "close_all"})
        _problem(status, payload, headers, "INSTRUMENTS_REJECTED")
        assert payload["rejections"] == [{"instrument": "SBER", "reason": "нет в составе"}]

    @pytest.mark.parametrize("path, body", [
        ("/api/v1/emulation/instruments", {}), ("/api/v1/emulation/instruments", {"instruments": []}),
        ("/api/v1/emulation/instruments", {"add": ["NG"]}), ("/api/v1/emulation/instruments/remove", {"mode": "x"}),
        ("/api/v1/emulation/instruments/remove", b"{bad"),
    ])
    def test_bad_body(self, controlled_server, path, body):
        srv, _, _ = controlled_server
        status, payload, headers = _call(srv, "POST", path, CONTROL_KEY, body)
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_not_running_matches_contract(self, controlled_server):
        srv, control, _ = controlled_server
        control.mark_exited(0)
        status, payload, headers = _call(srv, "POST", "/api/v1/emulation/instruments/remove", CONTROL_KEY,
                                         {"instruments": ["NG-12.26"]})
        _problem(status, payload, headers, "NOT_RUNNING")

    def test_list_and_one_member(self, controlled_server):
        srv, _, _ = controlled_server
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation/instruments", CONTROL_KEY)
        assert status == 200 and payload["revision"] == 1 and headers["ETag"] == '"1"'
        _check(payload, {"$ref": "#/components/schemas/MemberList"})
        status, member, _ = _call(srv, "GET", "/api/v1/emulation/instruments/ng-12.26", CONTROL_KEY)
        assert status == 200 and member["name"] == "NG-12.26"
        _check(member, {"$ref": "#/components/schemas/Member"})
        status, payload, headers = _call(srv, "GET", "/api/v1/emulation/instruments/GAZP", CONTROL_KEY)
        _problem(status, payload, headers, "NOT_FOUND")


class TestCoreApi:
    """GET /api/v1/core (add-contour-recovery) со ссылками."""

    STATE = {"state": "ready", "error": None, "attempts": 1, "next_retry_at": None,
             "version": "7.1.0", "pid": 4242, "started_at": "2026-10-24T09:00:00Z",
             "heartbeat": {"beat_at": "2026-10-24T09:00:05Z", "interval_secs": 5},
             "market_data": {"producer_id": "tbank-test", "max_change_id": 7,
                             "pairs": {"total": 2, "by_status": {"READY": 1, "BOOTSTRAPPING": 1}}},
             "quotas": {"quotes": {"per_minute": 3, "ceiling": 180, "lanes": {"live": 3}, "retry_after": None}}}

    def test_state_matches_contract(self, controlled_server):
        srv, _, _ = controlled_server
        srv.core_state = lambda: self.STATE
        status, payload, _ = _call(srv, "GET", "/api/v1/core", CONTROL_KEY)
        assert status == 200 and payload == {**self.STATE, "_links": {"self": {"href": "/api/v1/core"},
                                                                      "home": {"href": "/api/v1"}}}
        _check(payload, {"$ref": "#/components/schemas/Core"})

    def test_export_key_is_refused_and_missing_core_is_404(self, controlled_server):
        srv, _, _ = controlled_server
        srv.core_state = lambda: self.STATE
        assert _call(srv, "GET", "/api/v1/core", EXPORT_KEY)[0] == 401
        srv.core_state = None
        assert _call(srv, "GET", "/api/v1/core", CONTROL_KEY)[0] == 404


@pytest.fixture
def history_server(tmp_path):
    """Сервер с очередью прогонов истории (без потока очереди: прогоны остаются в queued до _fill)."""
    from src.assembly.history_runs import HistoryRuns
    from tests.unit.assembly.test_history_runs import FakeProcess

    FakeProcess.started = []
    control, store = _controlled(tmp_path)
    queue = HistoryRuns(store, tmp_path, max_runs=1, version="7.1.0", heartbeat_timeout=30,
                        share_strategies={"SBER": ["a"]}, future_strategies={"NG": ["a"]}, provider_factory=object,
                        prepare=lambda provider: (MarketDataSync(), None, "импорт не подтверждён"),
                        process_factory=FakeProcess, say=lambda line: None)
    control.history = queue
    srv = MarketDataExportServer(None, EXPORT_KEY, port=0, control=control, control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    yield srv, control, queue
    srv.close()


HISTORY = {"from": "2026-09-01 10:00", "to": "2026-09-08 10:00", "pause": 0, "instruments": ["NG"]}


class TestHistoryApi:
    """Прогоны истории — ресурс /api/v1/history (rework-contour-api)."""

    def test_history_is_queued_and_listed_with_limits(self, history_server):
        srv, _, queue = history_server
        status, payload, headers = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)
        assert status == 202 and payload["state"] == "queued"
        assert headers["Location"] == f"/api/v1/history/{payload['id']}" and not payload["id"].startswith("history-")
        _check(payload, {"$ref": "#/components/schemas/HistoryRun"})
        _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)
        queue._fill()
        status, listing, _ = _call(srv, "GET", "/api/v1/history", CONTROL_KEY)
        assert status == 200
        _check(listing, {"$ref": "#/components/schemas/HistoryRunList"})
        assert [item["state"] for item in listing["items"]] == ["running", "queued"]
        assert listing["limits"] == {"max": 1, "running": 1, "queued": 1}
        assert len(_call(srv, "GET", "/api/v1/history?state=finished", CONTROL_KEY)[1]["items"]) == 0
        status, one, _ = _call(srv, "GET", f"/api/v1/history/{payload['id']}", CONTROL_KEY)
        assert status == 200 and one["state"] == "running" and one["process"]["pid"] == 9001
        _check(one, {"$ref": "#/components/schemas/HistoryRun"})
        status, problem, headers = _call(srv, "GET", f"/api/v1/history/{payload['id']}/report", CONTROL_KEY)
        _problem(status, problem, headers, "REPORT_NOT_READY")

    @pytest.mark.parametrize("query", ["?state=account", "?limit=0", "?limit=201", "?limit=x"])
    def test_bad_list_query(self, history_server, query):
        srv, _, _ = history_server
        status, payload, headers = _call(srv, "GET", "/api/v1/history" + query, CONTROL_KEY)
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_finished_runs_are_limited(self, history_server):
        srv, _, queue = history_server
        for _ in range(3):
            run_id = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)[1]["id"]
            queue.stop(run_id)
        listing = _call(srv, "GET", "/api/v1/history?state=finished&limit=2", CONTROL_KEY)[1]
        assert [item["state"] for item in listing["items"]] == ["cancelled", "cancelled"]

    def test_history_is_accepted_while_core_is_not_ready_but_emulation_is_not(self, history_server):
        srv, control, _ = history_server
        control.mark_exited(0)
        control.accept_commands()
        control.core_status = lambda: {"state": "retrying", "error": "ConnectError", "attempts": 1,
                                       "next_retry_at": "2026-10-24T09:00:30Z", "version": "7.1.0", "pid": 1,
                                       "started_at": None, "heartbeat": None, "market_data": None, "quotas": {}}
        assert _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)[0] == 202
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "on"})
        _problem(status, payload, headers, "CORE_NOT_READY")
        assert payload["core"] == {"state": "retrying", "error": "ConnectError", "attempts": 1,
                                   "next_retry_at": "2026-10-24T09:00:30Z"}

    def test_emulation_refused_while_contour_folder_is_busy(self, history_server):
        srv, control, _ = history_server
        control.mark_exited(0)
        control.accept_commands()
        control.contour_busy = lambda: 4242
        status, payload, headers = _call(srv, "PUT", "/api/v1/emulation/target", CONTROL_KEY, {"mode": "on"})
        _problem(status, payload, headers, "CONTOUR_DIR_BUSY")
        assert payload["pid"] == 4242

    def test_stop_without_body(self, history_server):
        srv, _, _ = history_server
        run_id = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)[1]["id"]
        status, payload, _ = _call(srv, "POST", f"/api/v1/history/{run_id}/stop", CONTROL_KEY)
        assert status == 202 and payload["state"] == "cancelled"
        _check(payload, {"$ref": "#/components/schemas/HistoryRun"})
        status, payload, headers = _call(srv, "POST", f"/api/v1/history/{run_id}/stop", CONTROL_KEY, {"ignored": 1})
        _problem(status, payload, headers, "NOT_RUNNING")
        assert _call(srv, "POST", "/api/v1/history/20260101-000000/stop", CONTROL_KEY)[0] == 404
        assert _call(srv, "GET", "/api/v1/history/history-20260101-000000", CONTROL_KEY)[0] == 404

    def test_queued_run_offers_cancel_and_cancel_is_accepted(self, history_server):
        srv, _, _ = history_server
        run = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)[1]
        [action] = run["_actions"]
        assert (action["name"], action["method"], action["href"]) == ("cancel", "POST", f"/api/v1/history/{run['id']}/stop")
        assert _call(srv, action["method"], action["href"], CONTROL_KEY)[0] == 202

    def test_log_of_run(self, history_server):
        srv, _, queue = history_server
        run_id = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, HISTORY)[1]["id"]
        queue._fill()
        from src.assembly.history_runs import run_dir

        (run_dir(queue._data_dir, run_id) / "bot_debug.log").write_text("a\nb\nc\n", encoding="utf-8")
        status, text, _ = _text(srv, f"/api/v1/history/{run_id}/log?tail=2", CONTROL_KEY)
        assert status == 200 and text == "b\nc\n"
        assert _text(srv, "/api/v1/history/20260101-000000/log", CONTROL_KEY)[0] == 404

    def test_rejected_tokens(self, history_server):
        srv, _, _ = history_server
        status, payload, headers = _call(srv, "POST", "/api/v1/history", CONTROL_KEY, {**HISTORY, "instruments": ["NG", "AFLT"]})
        _problem(status, payload, headers, "INSTRUMENTS_REJECTED")
        assert payload["rejections"][0]["instrument"] == "AFLT"
