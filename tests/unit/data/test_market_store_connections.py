"""Короткие соединения рыночной базы закрываются сразу (fix-sqlite-connection-close).

ПРОМ v9.0.0, 16.11.2026: незакрытые соединения копились сотнями, и при пределе
256 открытых файлов ядро перестало открывать базы через 40 секунд после старта.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import sys

import pytest

from src.clusters.store import ClusterEventStore
from src.data.market_store import MarketDataStore

UID = "uid-1"


def _open_paths() -> list[str]:
    """Пути открытых процессом файлов: /proc на Linux, F_GETPATH на macOS."""
    if sys.platform.startswith("linux"):
        paths = []
        for name in os.listdir("/proc/self/fd"):
            try:
                paths.append(os.readlink(f"/proc/self/fd/{name}"))
            except OSError:
                pass
        return paths
    import fcntl

    paths = []
    for name in os.listdir("/dev/fd"):
        try:
            paths.append(fcntl.fcntl(int(name), fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0").decode())
        except OSError:
            pass
    return paths


def _base_files(path) -> int:
    """Открытые дескрипторы базы и её журнала WAL (``-shm`` — общая карта, его не считаем)."""
    name = os.path.basename(path)
    return sum(1 for item in _open_paths() if os.path.basename(item) in (name, name + "-wal"))


posix_only = pytest.mark.skipif(
    sys.platform == "win32" or not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="подсчёт открытых файлов процесса — только Linux и macOS",
)


@posix_only
def test_hundreds_of_short_operations_keep_no_files_open(tmp_path):
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    gc.disable()  # без явного close() файлы держал бы только сборщик мусора
    try:
        for step in range(250):
            store.save_live_state({"step": step}, kind="probe", source="tbank_exchange", instrument_uid=UID)
            assert store.live_state(kind="probe", source="tbank_exchange", instrument_uid=UID) == {"step": step}
        assert _base_files(store.path) == 0
    finally:
        gc.enable()


def test_connection_is_closed_after_operation(tmp_path):
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    with store._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_failed_operation_rolls_back_and_closes(tmp_path):
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    with pytest.raises(RuntimeError, match="сбой записи"):
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO metadata(key, value) VALUES ('probe', '1')")
            raise RuntimeError("сбой записи")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with store._connect() as check:
        assert check.execute("SELECT value FROM metadata WHERE key='probe'").fetchone() is None


def test_explicit_transaction_is_committed_on_success(tmp_path):
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO metadata(key, value) VALUES ('probe', '1')")
    with store._connect() as check:
        assert check.execute("SELECT value FROM metadata WHERE key='probe'").fetchone()[0] == "1"


def test_cluster_store_closes_connections(tmp_path):
    store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
    with store._connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
