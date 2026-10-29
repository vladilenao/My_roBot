"""Надзиратель контура на брокере (add-sandbox-contour): запуск, подъём после рестарта, подхват, выход."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from src.assembly.broker_supervisor import BrokerContourSupervisor
from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore, Member


class FakeProcess:
    started = []

    def __init__(self, kind, on_exit=None, **_):
        self.kind, self.on_exit, self.pid, self.exit_code = kind, on_exit, 7001, None
        self.stopped = False

    @property
    def running(self):
        return self.exit_code is None and not self.stopped

    def start(self):
        FakeProcess.started.append(self)
        return self.pid

    def stop(self):
        self.stopped = True

    def finish(self, code):
        self.exit_code = code
        self.on_exit(code, False)


class FakeAdopted(FakeProcess):
    def __init__(self, kind, pid, on_exit=None, **_):
        super().__init__(kind, on_exit)
        self.pid = pid


def _supervisor(tmp_path, store, autostart=True):
    FakeProcess.started = []
    return BrokerContourSupervisor(kind="sandbox", state_dir=tmp_path, core_state=store, version="9.9.9",
                                   autostart=autostart, heartbeat_timeout=30, heartbeat_secs=5,
                                   process_factory=FakeProcess, adopt_factory=FakeAdopted, say=lambda text: None)


def _launched(tmp_path) -> CoreStateStore:
    store = CoreStateStore(tmp_path / "core.sqlite3")
    store.record_launch("sandbox", "sandbox", [], source="api", members=[Member("future-ng-macd", {"id": "future-ng-macd"})])
    return store


def test_launch_writes_launch_file_and_starts_process(tmp_path):
    store = _launched(tmp_path)
    supervisor = _supervisor(tmp_path, store)
    assert supervisor.launch(store.desired("sandbox")) == 7001
    launch = json.loads((tmp_path / "contours/sandbox/launch.json").read_text(encoding="utf-8"))
    assert launch["contour_id"] == "sandbox" and launch["bindings"] == ["future-ng-macd"] and launch["generation"] == 1
    assert "token" not in json.dumps(launch).lower()
    assert supervisor.snapshot()["running"] and supervisor.snapshot()["recovery"] is None


def test_recover_autostarts_running_contour(tmp_path):
    store = _launched(tmp_path)
    supervisor = _supervisor(tmp_path, store)
    assert supervisor.recover() == "autostart"
    assert len(FakeProcess.started) == 1 and supervisor.recovery == "autostart"
    assert store.desired("sandbox").generation == 1, "автоподъём не создаёт поколение"


def test_recover_autostarts_with_empty_composition(tmp_path):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    store.record_launch("sandbox", "sandbox", [], source="api", members=[])
    supervisor = _supervisor(tmp_path, store)
    supervisor.recover()
    assert len(FakeProcess.started) == 1


def test_recover_waits_without_desired_or_autostart(tmp_path):
    supervisor = _supervisor(tmp_path, CoreStateStore(tmp_path / "core.sqlite3"))
    assert supervisor.recover() == "wait" and FakeProcess.started == [] and supervisor.ready
    supervisor = _supervisor(tmp_path, _launched(tmp_path), autostart=False)
    supervisor.recover()
    assert FakeProcess.started == []


def test_recover_adopts_live_contour(tmp_path):
    store = _launched(tmp_path)
    directory = tmp_path / "contours/sandbox"
    directory.mkdir(parents=True)
    (directory / "robot.pid").write_text(str(os.getpid()), encoding="utf-8")
    write_status(directory / "status.json", {"pid": os.getpid(), "version": "9.9.9", "state": "running",
                                             "alive_at": datetime.now(timezone.utc).isoformat()})
    supervisor = _supervisor(tmp_path, store)
    assert supervisor.recover() == "adopt"
    assert FakeProcess.started[0].pid == os.getpid() and supervisor.recovery == "adopted"


def test_exit_is_recorded_and_not_restarted(tmp_path):
    store = _launched(tmp_path)
    supervisor = _supervisor(tmp_path, store)
    supervisor.launch(store.desired("sandbox"))
    FakeProcess.started[0].finish(1)
    snapshot = supervisor.snapshot()
    assert snapshot["exit_code"] == 1 and snapshot["exited_at"] and not snapshot["running"]
    assert len(FakeProcess.started) == 1
