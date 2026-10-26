"""Прогоны истории — контуры вида history (add-history-contour)."""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.assembly import history_runs
from src.assembly.contour_control import STATUS_FILE, read_status, write_status
from src.assembly.core_state import (
    RUN_CANCELLED, RUN_FAILED, RUN_FINISHED, RUN_INTERRUPTED, RUN_QUEUED, RUN_RUNNING, RUN_STOPPED, RUN_STOPPING,
    CoreStateStore, read_history_stop,
)
from src.assembly.history_runs import HistoryProgress, HistoryRuns, clear_attempt, prepare_import, run_dir
from src.bot.launch import LaunchRequest
from src.bot.run_session import HistoricalRunSession
from src.config_loader import ConfigError, load_config
from src.history.report import MarketDataSync
from src.scheduler.clock import HistoricalClock

SHARES = {"SBER": ["a"]}
FUTURES = {"NG": ["a"], "BR": []}


def _key(run_id: str) -> str:
    """Ключ строки ``history_runs``: ``id`` API — имя папки, строка хранит его с префиксом (rework-contour-api)."""
    return history_runs.RUN_PREFIX + run_id


def _request(*tokens, start=datetime(2026, 9, 1, 10), end=datetime(2026, 9, 8, 10)):
    return LaunchRequest("history", "api", tokens or ("NG",), start=start, end=end, pause=0.0)


class FakeProcess:
    """Процесс прогона: пишет pid, выход — по ``exit``."""

    started: list = []

    def __init__(self, kind, *, command, on_exit, output):
        self.kind, self.command, self.on_exit, self.output = kind, command, on_exit, output
        self.stop_requested = False
        FakeProcess.started.append(self)

    def start(self):
        return 9000 + len(FakeProcess.started)

    def request_stop(self):
        self.stop_requested = True

    def stop(self):
        self.request_stop()
        self.exit(0)

    def exit(self, code):
        if not getattr(self, "_exited", False):
            self._exited = True
            self.on_exit(code, self.stop_requested)


@pytest.fixture
def runs(tmp_path):
    FakeProcess.started = []
    store = CoreStateStore(tmp_path / "core.sqlite3")
    syncs = []

    def prepare(provider):
        syncs.append(provider)
        return MarketDataSync(status="synchronized", snapshot_generation=42), 42, "импорт подтверждён: снимок 42"

    said = []
    queue = HistoryRuns(store, tmp_path, max_runs=2, version="7.1.0", heartbeat_timeout=30,
                        share_strategies=SHARES, future_strategies=FUTURES, provider_factory=object,
                        prepare=prepare, process_factory=FakeProcess, say=said.append)
    return SimpleNamespace(queue=queue, store=store, syncs=syncs, said=said, tmp_path=tmp_path)


class TestSubmit:
    def test_run_is_queued_with_id_and_folder(self, runs):
        status, payload = runs.queue.submit(_request("NG"))
        assert status == 202 and payload["state"] == RUN_QUEUED and "kind" not in payload
        assert not payload["id"].startswith("history-")
        assert runs.store.history_run(_key(payload["id"])).directory == payload["id"]
        assert payload["request"] == {"from": "2026-09-01 10:00", "to": "2026-09-08 10:00", "pause": 0.0,
                                      "instruments": ["NG"]}
        assert payload["queue_position"] == 1 and payload["progress"] is None and payload["process"] is None
        assert [action["name"] for action in payload["_actions"]] == ["cancel"]
        assert payload["_links"]["self"] == {"href": f"/api/v1/history/{payload['id']}"}

    def test_run_of_previous_version_is_found_by_folder_name(self, runs):
        runs.store.add_history_run("history-20261024-091000", "20261024-091000",
                                   {"from": "2026-09-01 10:00", "to": "2026-09-08 10:00", "pause": 1.0, "instruments": ["NG"]})
        run = runs.queue.run("20261024-091000")
        assert run["id"] == "20261024-091000" and run["state"] == RUN_QUEUED
        assert [item["id"] for item in runs.queue.runs("active")] == ["20261024-091000"]
        assert runs.queue.runs("finished") == []

    def test_same_second_gets_number(self, runs):
        first = runs.queue.submit(_request())[1]["id"]
        second = runs.queue.submit(_request())[1]["id"]
        assert second == first + "-2"

    @pytest.mark.parametrize("tokens, token", [(("NG", "AFLT"), "AFLT"), (("BR",), "BR"), (("NG", "NG-12.26"), "NG-12.26")])
    def test_tokens_without_assignments_or_conflict_are_refused(self, runs, tokens, token):
        status, payload = runs.queue.submit(_request(*tokens))
        assert status == 422 and token in [item["token"] for item in payload["rejections"]]
        assert runs.store.history_runs() == []


class TestQueue:
    def test_third_run_waits_and_starts_after_exit(self, runs):
        ids = [runs.queue.submit(_request())[1]["id"] for _ in range(3)]
        runs.queue._fill()
        assert [p.command[-1] for p in FakeProcess.started] == [run_dir(runs.tmp_path, i).name for i in ids[:2]]
        assert runs.queue.run(ids[2])["queue_position"] == 1
        assert runs.queue.limits() == {"max": 2, "running": 2, "queued": 1}
        assert len(runs.syncs) == 2                       # сверка — на каждый старт, по одной
        FakeProcess.started[0].exit(0)
        runs.queue._fill()
        assert len(FakeProcess.started) == 3 and runs.store.history_run(_key(ids[0])).state == RUN_FINISHED
        assert runs.store.history_run(_key(ids[2])).state == RUN_RUNNING

    def test_launch_file_carries_snapshot_and_request(self, runs):
        run_id = runs.queue.submit(_request("NG", "SBER"))[1]["id"]
        runs.queue._fill()
        process = FakeProcess.started[0]
        launch = json.loads((run_dir(runs.tmp_path, run_id) / "launch.json").read_text(encoding="utf-8"))
        assert launch["contour_id"] == _key(run_id) and launch["kind"] == "history"
        assert launch["snapshot_generation"] == 42 and launch["market_data_sync"]["status"] == "synchronized"
        assert launch["instruments"] == ["NG", "SBER"] and launch["from"] == "2026-09-01 10:00"
        assert process.command[-3:] == ["--contour", "history", run_dir(runs.tmp_path, run_id).name]
        assert process.output == run_dir(runs.tmp_path, run_id) / "console.log"
        run = runs.queue.run(run_id)
        assert run["state"] == RUN_RUNNING and run["process"]["pid"] == 9001
        assert run["process"]["dir"] == f"contours/history/{run_id}"
        assert [action["name"] for action in run["_actions"]] == ["stop"]
        assert run["market_data_sync"] == {"status": "synchronized", "snapshot_generation": 42}
        assert any("запущен" in line for line in runs.said)

    @pytest.mark.parametrize("code, stop, state, reason", [
        (0, False, RUN_FINISHED, None),
        (0, True, RUN_STOPPED, "остановлено оператором"),
        (2, False, RUN_FAILED, "NG: нет свечей"),
        (1, False, RUN_FAILED, "процесс прогона завершился с кодом 1"),
    ])
    def test_exit_outcome(self, runs, code, stop, state, reason):
        run_id = runs.queue.submit(_request())[1]["id"]
        runs.queue._fill()
        if code == 2:
            write_status(run_dir(runs.tmp_path, run_id) / STATUS_FILE, {"reason": "NG: нет свечей"})
        if stop:
            runs.queue.stop(run_id)
        FakeProcess.started[0].exit(code)
        run = runs.queue.run(run_id)
        assert (run["state"], run["process"]["exit_code"], run["reason"], run["process"]["pid"]) == (state, code, reason, None)
        assert run["_actions"] == [] and run["process"]["exited_at"] == run["finished_at"]

    def test_result_and_progress_come_from_run_files(self, runs):
        run_id = runs.queue.submit(_request())[1]["id"]
        runs.queue._fill()
        directory = run_dir(runs.tmp_path, run_id)
        write_status(directory / STATUS_FILE, {"phase": "running", "start": "2026-09-01T10:00:00Z",
                                               "end": "2026-09-08T10:00:00Z", "market_now": "2026-09-04T12:00:00Z",
                                               "ticks": 4440})
        assert runs.queue.run(run_id)["progress"]["fraction"] == pytest.approx(74 / 168, abs=1e-4)
        assert runs.queue.report(run_id) == (404, {"code": "REPORT_NOT_READY",
                                                   "message": "Прогон ещё не завершён или отчёт не записан."})
        assert "report" not in runs.queue.run(run_id)["_links"]
        (directory / "report.json").write_text(json.dumps({
            "covered": True, "stop_reason": "диапазон обработан полностью",
            "result": {"trades": 7, "net_pnl": "12.5", "fees": "1", "balance": "100012.5", "open_positions": [],
                       "fills": 9}}), encoding="utf-8")
        FakeProcess.started[0].exit(0)
        rub = lambda amount: {"amount": amount, "currency": "RUB"}  # noqa: E731
        assert runs.queue.report(run_id) == (200, {
            "covered": True, "stop_reason": "диапазон обработан полностью", "trades": 7, "net_pnl": rub("12.5"),
            "fees": rub("1"), "balance": rub("100012.5"), "open_positions": [],
            "_links": {"run": {"href": f"/api/v1/history/{run_id}"}}})
        assert runs.queue.run(run_id)["_links"]["report"] == {"href": f"/api/v1/history/{run_id}/report"}

    def test_failed_process_start_frees_slot(self, runs):
        class Broken(FakeProcess):
            def start(self):
                raise OSError("нет исполняемого файла")

        runs.queue._process_factory = Broken
        run_id = runs.queue.submit(_request())[1]["id"]
        runs.queue._fill()
        run = runs.queue.run(run_id)
        assert run["state"] == RUN_FAILED and "OSError" in run["reason"]
        assert runs.queue.limits()["running"] == 0

    def test_stop_during_import_check_cancels_start(self, runs):
        run_id = runs.queue.submit(_request())[1]["id"]

        def prepare(provider):
            runs.store.update_history_run(_key(run_id), stop_requested=True, state=RUN_STOPPING)
            return MarketDataSync(), None, "импорт не подтверждён"

        runs.queue._prepare = prepare
        runs.queue._fill()
        assert FakeProcess.started == [] and runs.store.history_run(_key(run_id)).state == RUN_CANCELLED

    def test_queue_thread_starts_runs(self, runs):
        runs.queue.start()
        try:
            run_id = runs.queue.submit(_request())[1]["id"]
            for _ in range(200):
                if runs.store.history_run(_key(run_id)).state == RUN_RUNNING:
                    break
                threading.Event().wait(0.01)
            assert runs.store.history_run(_key(run_id)).state == RUN_RUNNING
        finally:
            runs.queue.close()


class TestStop:
    def test_queued_run_is_cancelled(self, runs):
        run_id = runs.queue.submit(_request())[1]["id"]
        status, payload = runs.queue.stop(run_id)
        assert status == 202 and payload["state"] == RUN_CANCELLED and payload["_actions"] == []
        runs.queue._fill()
        assert FakeProcess.started == []

    def test_running_run_gets_flag_and_stdin_stop(self, runs):
        run_id = runs.queue.submit(_request())[1]["id"]
        runs.queue._fill()
        status, payload = runs.queue.stop(run_id)
        assert status == 202 and payload["state"] == RUN_STOPPING
        assert FakeProcess.started[0].stop_requested and read_history_stop(runs.store.path, _key(run_id))
        status, payload = runs.queue.stop(run_id)
        assert status == 200 and payload["state"] == RUN_STOPPING

    def test_finished_and_unknown(self, runs):
        run_id = runs.queue.submit(_request())[1]["id"]
        runs.queue._fill()
        FakeProcess.started[0].exit(0)
        assert runs.queue.stop(run_id)[0] == 409 and runs.queue.stop(run_id)[1]["code"] == "NOT_RUNNING"
        assert runs.queue.stop("20260101-000000")[0] == 404

    def test_robot_stop_cancels_queue_and_stops_runs(self, runs):
        ids = [runs.queue.submit(_request())[1]["id"] for _ in range(3)]
        runs.queue._fill()
        runs.queue.close()
        states = [runs.store.history_run(_key(run_id)) for run_id in ids]
        assert [run.state for run in states] == [RUN_STOPPED, RUN_STOPPED, RUN_CANCELLED]
        assert states[2].reason == "робот остановлен"


class TestRecover:
    def test_queue_is_not_resumed_and_dead_runs_are_interrupted(self, runs):
        queued = runs.store.add_history_run("history-a", "a", {}).run_id
        running = runs.store.add_history_run("history-b", "b", {}).run_id
        runs.store.update_history_run(running, state=RUN_RUNNING, pid=999999)
        runs.queue.recover()
        assert runs.store.history_run(queued).state == RUN_CANCELLED
        assert runs.store.history_run(queued).reason == "робот перезапущен"
        assert runs.store.history_run(running).state == RUN_INTERRUPTED

    def test_live_run_is_adopted_and_counts_in_queue(self, runs):
        adopted = []

        class Adopted:
            def __init__(self, kind, pid, *, on_exit):
                self.on_exit = on_exit
                adopted.append(pid)

            def start(self):
                return None

            def request_stop(self):
                pass

            def stop(self):
                pass

        run_id = runs.store.add_history_run("history-a", "a", {}).run_id
        runs.store.update_history_run(run_id, state=RUN_RUNNING, pid=4242)
        directory = run_dir(runs.tmp_path, run_id)
        directory.mkdir(parents=True)
        (directory / "robot.pid").write_text("4242", encoding="utf-8")
        write_status(directory / STATUS_FILE, {"pid": 4242, "alive_at": datetime.now(timezone.utc).isoformat()})
        runs.queue._adopt_factory = Adopted
        runs.queue._alive = lambda pid: True
        runs.queue.recover()
        assert adopted == [4242] and runs.queue.limits()["running"] == 1
        write_status(directory / STATUS_FILE, {"pid": 4242, "exited_at": "x", "exit_code": 0})
        runs.queue._processes[run_id].on_exit(None, False)
        assert runs.store.history_run(run_id).state == RUN_FINISHED


def test_prepare_import_messages():
    class Provider:
        def __init__(self, answer):
            self.answer = answer

        def prepare_snapshot(self):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    from src.api.emulator_client import EmulatorError

    sync, generation, line = prepare_import(Provider({"producer_id": "p", "target_change_id": 12, "after_id": 12,
                                                       "synchronized": True, "snapshot_generation": 42}))
    assert (sync.status, generation) == ("synchronized", 42) and "снимок 42" in line
    sync, generation, line = prepare_import(Provider(EmulatorError("SOURCE_UNAVAILABLE", "My Robot недоступен")))
    assert (sync.status, generation) == ("unconfirmed", None) and "не подтверждён" in line
    sync, _, line = prepare_import(Provider({"producer_id": "p", "target_change_id": 12, "after_id": 3,
                                             "synchronized": False, "source": {"state": "blocked"}}))
    assert sync.status == "unconfirmed" and "blocked" in line


def test_clear_attempt_keeps_launch_status_and_logs(tmp_path):
    for name in ("launch.json", "status.json", "console.log", "robot.pid", "bot_debug.log", "bot_debug.log.2",
                 "trades.sqlite3", "contract_chain.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "telegram-diagnostics").mkdir()
    clear_attempt(tmp_path, "bot_debug.log")
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "bot_debug.log", "bot_debug.log.2", "console.log", "launch.json", "robot.pid", "status.json"]


class TestProgress:
    def _progress(self, tmp_path, **kw):
        store = CoreStateStore(tmp_path / "core.sqlite3")
        store.add_history_run("history-a", "a", {})
        progress = HistoryProgress(tmp_path, run_id="history-a", core_db=store.path, version="7.1.0", interval=5,
                                   pid=77, **kw)
        clock = HistoricalClock(datetime(2026, 9, 1, 10), datetime(2026, 9, 1, 12), timedelta(minutes=1), 0)
        session = HistoricalRunSession(clock)
        return progress, store, clock, session

    def test_status_follows_phases_and_finish(self, tmp_path):
        progress, _, clock, session = self._progress(tmp_path)
        progress.attach(clock, session)
        status = read_status(tmp_path / STATUS_FILE)
        assert (status["phase"], status["pid"], status["start"], status["end"]) == (
            "checking", 77, "2026-09-01T10:00:00Z", "2026-09-01T12:00:00Z")
        progress.phase("running")
        progress.reject([SimpleNamespace(token="NG-03.20", reason="нет в эмуляторе")])
        progress.finish(2)
        status = read_status(tmp_path / STATUS_FILE)
        assert status["phase"] == "running" and status["exit_code"] == 2 and status["exited_at"]
        assert status["rejections"] == [{"token": "NG-03.20", "reason": "нет в эмуляторе"}]
        assert status["exit_reason"] == "NG-03.20: нет в эмуляторе"

    def test_flag_and_stopped_beat_stop_session_but_lost_beat_does_not(self, tmp_path):
        progress, store, clock, session = self._progress(tmp_path)
        progress.attach(clock, session)
        store.beat(1, "7.2.0", "s")
        progress.check()                                  # чужая версия и пульс — не повод
        assert not session.should_stop()
        store.update_history_run("history-a", stop_requested=True)
        progress.check()
        assert session.should_stop() and session.stop_reason() == "остановлено оператором"

    def test_robot_stop_and_stdin_stop(self, tmp_path):
        progress, store, clock, session = self._progress(tmp_path)
        store.beat(1, "7.1.0", "s")
        store.mark_stopped(1)
        progress.check()                                  # сессии ещё нет: стоп ждёт её
        progress.attach(clock, session)
        assert session.should_stop()
        event = threading.Event()
        (tmp_path / "b").mkdir()
        other, _, clock2, session2 = self._progress(tmp_path / "b", stop_event=event)
        other.attach(clock2, session2)
        other.start()
        event.set()
        for _ in range(100):
            if session2.should_stop():
                break
            threading.Event().wait(0.02)
        other.finish(0)
        assert session2.should_stop() and read_status(tmp_path / "b" / STATUS_FILE)["exit_reason"] == "остановлено оператором"


class TestSettings:
    def _load(self, tmp_path, text):
        path = tmp_path / "robot.toml"
        path.write_text(text, encoding="utf-8")
        from src.config import _DEFAULTS

        return load_config(_DEFAULTS, config_file=path)

    def test_default_and_value(self, tmp_path):
        assert self._load(tmp_path, "")["contours_max_history_runs"] == 2
        assert self._load(tmp_path, "[contours]\nmax_history_runs = 4\n")["contours_max_history_runs"] == 4

    @pytest.mark.parametrize("value", ["0", "9", "true", "1.5"])
    def test_refused(self, tmp_path, value):
        with pytest.raises(ConfigError, match="max_history_runs"):
            self._load(tmp_path, f"[contours]\nmax_history_runs = {value}\n")


def test_module_is_history_runs():
    assert history_runs.RUN_PREFIX == "history-"
