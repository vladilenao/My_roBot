"""Unit-тесты блокировки единственного боевого экземпляра робота."""

import os

import pytest

from src.runtime_lock import LOCK_FILE, RuntimeLockError, acquire_runtime_lock


class TestRuntimeLock:
    def test_writes_own_pid(self, tmp_path) -> None:
        path = acquire_runtime_lock(tmp_path)

        assert path == tmp_path / LOCK_FILE
        assert path.read_text(encoding="utf-8") == str(os.getpid())

    def test_second_acquire_with_live_pid_fails(self, tmp_path) -> None:
        (tmp_path / LOCK_FILE).write_text(str(os.getpid()), encoding="utf-8")

        with pytest.raises(RuntimeLockError) as excinfo:
            acquire_runtime_lock(tmp_path)

        message = str(excinfo.value)
        assert "уже запущен" in message
        assert str(os.getpid()) in message

    def test_stale_pid_is_replaced(self, tmp_path) -> None:
        (tmp_path / LOCK_FILE).write_text("1073741824", encoding="utf-8")

        path = acquire_runtime_lock(tmp_path)

        assert path.read_text(encoding="utf-8") == str(os.getpid())

    def test_unreadable_lock_file_is_replaced(self, tmp_path) -> None:
        (tmp_path / LOCK_FILE).write_text("не-число", encoding="utf-8")

        path = acquire_runtime_lock(tmp_path)

        assert path.read_text(encoding="utf-8") == str(os.getpid())

    def test_empty_lock_file_is_replaced(self, tmp_path) -> None:
        (tmp_path / LOCK_FILE).write_text("", encoding="utf-8")

        path = acquire_runtime_lock(tmp_path)

        assert path.read_text(encoding="utf-8") == str(os.getpid())
