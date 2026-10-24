"""Процесс контура применяет желаемое ядра на границе тика (add-control-api)."""

import json
import logging
import sqlite3

import pytest

from src.assembly.contour_control import ContourControl, read_status
from src.assembly.core_state import CoreStateStore


class Manager:
    """Менеджер сделок: режим остановки и остаток сделок."""

    def __init__(self, positions=0, entries=0):
        self.remainder = (positions, entries)
        self.calls = []

    def stop_contour(self, mode):
        self.calls.append(mode)
        return ()

    def contour_remainder(self):
        return self.remainder


@pytest.fixture
def env(tmp_path):
    core = CoreStateStore(tmp_path / "core.sqlite3")
    core.record_launch("emulation", "emulation", [{"token": "NG"}])
    stops = []

    def control():
        return ContourControl(core_db=tmp_path / "core.sqlite3", contour_id="emulation",
                              journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                              request_stop=stops.append, version="7.1.0", pid=4242)
    return core, control, stops, tmp_path


def saved(tmp_path):
    with sqlite3.connect(tmp_path / "trades.sqlite3") as conn:
        return conn.execute("SELECT applied_generation, state, stop_mode FROM contour_state").fetchone()


def test_running_contour_reports_generation_in_status(env):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    contour.before_tick()
    contour.after_tick()
    status = read_status(tmp_path / "status.json")
    assert (status["state"], status["applied_generation"], status["open_positions"]) == ("running", 1, 1)
    assert status["pid"] == 4242 and status["last_tick_at"] and stops == []
    assert manager.calls == [None]


def test_no_entries_with_open_position_drains_then_stops(env):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    core.record_stop("emulation", "no_entries")

    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopping_no_entries" and saved(tmp_path) == (2, "stopping_no_entries", "no_entries")
    assert stops == []

    # Следующий тик: остановка повторяется, позиция закрылась по стопу.
    manager.remainder = (0, 0)
    contour.before_tick()
    contour.after_tick()
    assert manager.calls == [None, "no_entries", "no_entries"]
    assert saved(tmp_path) == (2, "stopped", "no_entries") and stops == ["остановка командой ядра"]
    assert read_status(tmp_path / "status.json")["state"] == "stopped"


def test_stop_without_trades_finishes_on_first_tick(env):
    core, control, stops, _ = env
    contour = control()
    contour.start(Manager())
    core.record_stop("emulation", "close_all")
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopped" and len(stops) == 1


def test_restarted_process_applies_current_stop_again(env):
    core, control, stops, tmp_path = env
    core.record_stop("emulation", "close_all")
    first = control()
    first.start(Manager(positions=1))
    # Процесс перезапущен посреди остановки: поколение уже применено, но блок — в памяти менеджера.
    manager = Manager(positions=1)
    control().start(manager)
    assert manager.calls == ["close_all"] and saved(tmp_path) == (2, "stopping_close_all", "close_all")


def test_launch_after_stop_lifts_it(env):
    core, control, stops, _ = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    core.record_stop("emulation", "no_entries")
    contour.before_tick()
    core.record_launch("emulation", "emulation", [{"token": "BR"}])
    contour.before_tick()
    assert manager.calls[-1] is None and contour.state == "running" and contour.applied_generation == 3


def test_unreadable_core_database_keeps_last_desired(env, caplog):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    (tmp_path / "core.sqlite3").write_bytes(b"not a database at all" * 100)
    with caplog.at_level(logging.WARNING, logger="src.assembly.contour_control"):
        contour.before_tick()
        contour.before_tick()
    assert contour.state == "running" and contour.applied_generation == 1
    assert sum("База ядра не читается" in record.getMessage() for record in caplog.records) == 1


def test_contour_without_core_database_trades_as_before(tmp_path):
    stops = []
    contour = ContourControl(core_db=tmp_path / "missing.sqlite3", contour_id="emulation",
                             journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                             request_stop=stops.append)
    manager = Manager()
    contour.start(manager)
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "running" and manager.calls == [] and stops == []


def test_notify_only_contour_stops_at_once(env):
    core, control, stops, _ = env
    contour = control()
    contour.start(None)
    core.record_stop("emulation", "no_entries")
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopped" and len(stops) == 1


def test_broken_status_is_not_an_error(tmp_path):
    (tmp_path / "status.json").write_text("{broken", encoding="utf-8")
    assert read_status(tmp_path / "status.json") is None
    (tmp_path / "status.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert read_status(tmp_path / "status.json") is None
    assert read_status(tmp_path / "absent.json") is None
