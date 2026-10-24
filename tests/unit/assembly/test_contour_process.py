"""Процесс контура: файл запуска, переезд журнала, надзор через stdin (add-contour-process, D2, D5, D6)."""

import ast
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.assembly import process
from src.assembly.process import (
    ContourProcess, JournalMigrationError, LaunchFileError, StdinStop, contour_dir, migrate_journal, read_launch,
    write_launch,
)


class TestLaunchFile:
    def test_round_trip(self, tmp_path):
        write_launch(tmp_path, {"contour_id": "emulation", "instruments": []})

        assert read_launch(tmp_path, "emulation")["schema_version"] == 1

    def test_missing_file(self, tmp_path):
        with pytest.raises(LaunchFileError, match="запускает ядро"):
            read_launch(tmp_path, "emulation")

    def test_other_schema_version(self, tmp_path):
        (tmp_path / "launch.json").write_text(json.dumps({"schema_version": 2, "contour_id": "emulation"}))

        with pytest.raises(LaunchFileError, match="разных версий"):
            read_launch(tmp_path, "emulation")

    def test_other_contour(self, tmp_path):
        write_launch(tmp_path, {"contour_id": "account"})

        with pytest.raises(LaunchFileError, match="account"):
            read_launch(tmp_path, "emulation")


def _journal(data_dir):
    with sqlite3.connect(data_dir / "trades.sqlite3") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE trades (trade_id TEXT)")
        conn.execute("INSERT INTO trades VALUES ('ng-open')")
    (data_dir / "trade_event.csv").write_text("событие\n", encoding="utf-8")
    (data_dir / "trade_summary.csv").write_text("карточка\n", encoding="utf-8")
    (data_dir / "trade_decision_trace.log").write_text("трасса\n", encoding="utf-8")
    (data_dir / "trade_decision_trace.log.1").write_text("старая трасса\n", encoding="utf-8")
    (data_dir / "telegram-diagnostics").mkdir()
    (data_dir / "telegram-diagnostics" / "probe.json").write_text("{}", encoding="utf-8")


def _snapshot(directory):
    """Файлы журнала и строки базы; служебный индекс WAL (-shm) меняется при любом чтении."""
    files = {path.relative_to(directory): path.read_bytes() for path in sorted(directory.rglob("*"))
             if path.is_file() and "contours" not in path.parts and not path.name.endswith(("-shm", "-wal"))
             and path.suffix != ".sqlite3"}
    with sqlite3.connect(directory / "trades.sqlite3") as conn:
        files["trades"] = conn.execute("SELECT trade_id FROM trades").fetchall()
    return files


class TestJournalMigration:
    def test_first_start_copies_journal_and_keeps_source(self, tmp_path):
        _journal(tmp_path)
        before = _snapshot(tmp_path)
        target = contour_dir(tmp_path, "emulation")

        assert migrate_journal(tmp_path, target, version="7.0.0") is True

        with sqlite3.connect(target / "trades.sqlite3") as conn:
            assert conn.execute("SELECT trade_id FROM trades").fetchall() == [("ng-open",)]
        for name in ("trade_event.csv", "trade_summary.csv", "trade_decision_trace.log", "trade_decision_trace.log.1"):
            assert (target / name).read_text(encoding="utf-8") == (tmp_path / name).read_text(encoding="utf-8")
        assert (target / "telegram-diagnostics" / "probe.json").exists()
        record = json.loads((target / "migrated-from.json").read_text(encoding="utf-8"))
        assert record["from"] == str(tmp_path) and record["version"] == "7.0.0"
        assert _snapshot(tmp_path) == before

    def test_second_start_does_not_copy(self, tmp_path):
        _journal(tmp_path)
        target = contour_dir(tmp_path, "emulation")
        migrate_journal(tmp_path, target, version="7.0.0")

        assert migrate_journal(tmp_path, target, version="7.0.0") is False

    def test_nothing_to_migrate(self, tmp_path):
        assert migrate_journal(tmp_path, contour_dir(tmp_path, "emulation"), version="7.0.0") is False

    def test_failed_copy_removes_partial_folder(self, tmp_path, monkeypatch):
        _journal(tmp_path)
        before = _snapshot(tmp_path)
        target = contour_dir(tmp_path, "emulation")

        def broken(*args, **kwargs):
            raise OSError("диск переполнен")

        monkeypatch.setattr(process.shutil, "copy2", broken)
        with pytest.raises(JournalMigrationError, match="не перенесён"):
            migrate_journal(tmp_path, target, version="7.0.0")

        assert not target.exists()
        assert not target.with_name("emulation.migrating").exists()
        assert _snapshot(tmp_path) == before


def _script(code):
    return [sys.executable, "-c", code]


class TestContourProcess:
    def test_stop_line_ends_process_after_tick(self):
        exits = []
        child = ContourProcess("emulation", on_exit=lambda code, stopping: exits.append((code, stopping)),
                               command=_script("import sys\nfor line in sys.stdin:\n    if line.strip()=='stop': break\n"))
        pid = child.start()
        assert pid and child.running and child.pid == pid

        assert child.stop() == 0
        assert exits == [(0, True)] and child.pid is None

    def test_crash_is_reported(self):
        exits = []
        child = ContourProcess("emulation", on_exit=lambda code, stopping: exits.append((code, stopping)),
                               command=_script("raise SystemExit(1)"))
        child.start()
        deadline = time.monotonic() + 10
        while child.running and time.monotonic() < deadline:
            time.sleep(0.05)

        assert child.exit_code == 1 and exits == [(1, False)]

    def test_stuck_process_is_killed_after_timeout(self):
        child = ContourProcess("emulation", stop_timeout=0.5,
                               command=_script("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                               "import sys\nsys.stdin.close()\ntime.sleep(60)"))
        child.start()
        time.sleep(0.3)

        assert child.stop() not in (0, None)


class TestStdinStop:
    def test_stop_command(self):
        stop = StdinStop(io.StringIO("ping\nstop\n"))
        stop._read()

        assert stop.requested.is_set() and stop.reason == "команда ядра"

    def test_end_of_stream_means_core_is_gone(self):
        stop = StdinStop(io.StringIO(""))
        stop._read()

        assert stop.requested.is_set() and stop.reason == "ядро завершилось"

    def test_first_reason_wins(self):
        stop = StdinStop(io.StringIO(""))
        stop.request("Ctrl+C")
        stop._read()

        assert stop.reason == "Ctrl+C"


def test_command_uses_same_entry(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert process.contour_command("emulation") == [sys.executable, "--contour", "emulation"]
    monkeypatch.setattr(sys, "frozen", False)
    command = process.contour_command("emulation")
    assert command[1].endswith("run.py") and command[2:] == ["--contour", "emulation"]



# Ядро запускает контур, пока его потоки держат вызовы gRPC брокера.
_GRPC_PROBE = """
import sys, threading, time
if sys.argv[1] == "fixed":
    import src.grpc_fork
import grpc
from src.assembly.process import ContourProcess
call = grpc.insecure_channel("127.0.0.1:1").unary_unary("/probe.Probe/Call")
def busy():
    while True:
        try:
            call(b"", timeout=0.05, wait_for_ready=True)
        except grpc.RpcError:
            pass
for _ in range(4):
    threading.Thread(target=busy, daemon=True).start()
time.sleep(0.5)
for _ in range(3):
    contour = ContourProcess("emulation", command=[sys.executable, "-c", "import sys; sys.stdin.read()"])
    contour.start()
    contour.stop()
"""
ROOT = Path(__file__).resolve().parents[3]


class TestGrpcForkHandlers:
    @staticmethod
    def _stderr(mode):
        env = {key: value for key, value in os.environ.items() if key != "GRPC_ENABLE_FORK_SUPPORT"}
        result = subprocess.run([sys.executable, "-c", _GRPC_PROBE, mode], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr[-500:]
        return result.stderr

    def test_contour_start_does_not_flood_core_console(self):
        if "fork_posix" not in self._stderr("plain"):
            pytest.skip("gRPC этой платформы не печатает обработчики fork()")
        output = self._stderr("fixed")
        assert "fork_posix" not in output and "ev_poll_posix" not in output

    def test_run_imports_module_before_grpc(self):
        tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
        first = next(node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)))
        assert isinstance(first, ast.Import) and first.names[0].name == "src.grpc_fork"
