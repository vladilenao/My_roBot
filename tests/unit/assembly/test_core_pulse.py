"""Пульс ядра в core.sqlite3 и связь контура с ним (add-contour-recovery)."""

import sqlite3

import pytest

from src.assembly.core_state import CoreStateStore, read_heartbeat
from src.assembly.pulse import LINK_LOST, LINK_OK, STOP_CORE_STOPPED, STOP_CORE_VERSION, CorePulse, CorePulseWriter
from src.config_loader import ConfigError, load_config


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    return CoreStateStore(tmp_path / "core.sqlite3")


def _pulse(store, clock, *, version="7.1.0"):
    stops, checks = [], []
    pulse = CorePulse(store.path, version=version, interval=5, timeout=30, on_stop=stops.append,
                      on_check=checks.append, clock=clock)
    return pulse, stops, checks


class TestHeartbeatStore:
    def test_beat_and_stop(self, store):
        store.beat(4242, "7.1.0", "2026-10-24T09:00:00Z")
        beat = read_heartbeat(store.path)
        assert (beat.core_pid, beat.core_version, beat.state, beat.stopped) == (4242, "7.1.0", "running", False)
        assert 0 <= beat.age() < 5
        store.mark_stopped(4242)
        assert read_heartbeat(store.path).stopped

    def test_new_core_overwrites_and_old_core_cannot_stop_it(self, store):
        store.beat(1, "7.1.0", "2026-10-24T09:00:00Z")
        store.beat(2, "7.2.0", "2026-10-24T09:05:00Z")
        store.mark_stopped(1)
        beat = store.heartbeat()
        assert (beat.core_pid, beat.core_version, beat.state) == (2, "7.2.0", "running")

    def test_missing_base_or_table(self, tmp_path):
        assert read_heartbeat(tmp_path / "absent.sqlite3") is None
        old = tmp_path / "old.sqlite3"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE contours (contour_id TEXT)")
        assert read_heartbeat(old) is None

    def test_writer_beats_first_and_marks_stopped_once(self, store):
        writer = CorePulseWriter(store, pid=4242, version="7.1.0", interval=60)
        writer.start()
        assert store.heartbeat().state == "running" and writer.payload()["interval_secs"] == 60
        writer.stop()
        writer.stop()
        assert store.heartbeat().stopped


class TestContourPulse:
    def test_lost_after_timeout_and_back(self, store):
        clock = Clock()
        store.beat(4242, "7.1.0", "s")
        pulse, stops, checks = _pulse(store, clock)
        assert pulse.check() == LINK_OK
        # Ядро убито: удар больше не обновляется, часы контура идут.
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE heartbeat SET beat_at='2026-01-01T00:00:00Z'")
        clock.now = 29
        assert pulse.check() == LINK_OK
        clock.now = 31
        assert pulse.check() == LINK_LOST
        # Новое ядро той же версии.
        store.beat(5151, "7.1.0", "s2")
        assert pulse.check() == LINK_OK
        assert stops == [] and checks == [LINK_OK, LINK_OK, LINK_LOST, LINK_OK]

    def test_unreadable_or_missing_base_is_lost_after_timeout(self, tmp_path):
        clock = Clock()
        pulse = CorePulse(tmp_path / "absent.sqlite3", version="7.1.0", interval=5, timeout=30,
                          on_stop=lambda reason: None, clock=clock)
        assert pulse.check() == LINK_OK
        clock.now = 31
        assert pulse.check() == LINK_LOST

    def test_stopped_core_stops_contour_once(self, store):
        store.beat(4242, "7.1.0", "s")
        store.mark_stopped(4242)
        pulse, stops, _ = _pulse(store, Clock())
        pulse.check()
        pulse.check()
        assert stops == [STOP_CORE_STOPPED]

    def test_core_of_other_version_stops_contour(self, store):
        store.beat(4242, "7.2.0", "s")
        pulse, stops, _ = _pulse(store, Clock(), version="7.1.0")
        pulse.check()
        assert stops == [STOP_CORE_VERSION]

    def test_stale_beat_of_other_version_does_not_stop(self, store):
        store.beat(4242, "7.2.0", "s")
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE heartbeat SET beat_at='2026-01-01T00:00:00Z'")
        pulse, stops, _ = _pulse(store, Clock(), version="7.1.0")
        pulse.check()
        assert stops == []


class TestSettings:
    def _load(self, tmp_path, text):
        path = tmp_path / "robot.toml"
        path.write_text(text, encoding="utf-8")
        return load_config({}, config_file=path)

    def test_values(self, tmp_path):
        config = self._load(tmp_path, "[contours]\nheartbeat_secs = 2\nheartbeat_timeout_secs = 10\nautostart = false\n")
        assert (config["contours_heartbeat_secs"], config["contours_heartbeat_timeout_secs"],
                config["contours_autostart"]) == (2, 10, False)

    @pytest.mark.parametrize("text", ["heartbeat_secs = 30\n", "heartbeat_timeout_secs = 5\n",
                                      "heartbeat_secs = 0\n", "autostart = 1\n"])
    def test_refused(self, tmp_path, text):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "[contours]\n" + text)
