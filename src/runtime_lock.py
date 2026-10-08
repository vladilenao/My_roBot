"""Защита от одновременного запуска двух боевых роботов.

Два процесса в одном каталоге состояния делят журналы, delivery-состояние и
rate-limits API, а их уведомления дублируются. Боевой запуск берёт PID-файл
``robot.pid`` в ``state_dir``: занятый живым процессом файл — ошибка запуска,
мёртвый PID — остаток прошлого запуска и повод перезаписать файл.

Исторические прогоны каталогом не делятся (у каждого свой одноразовый
``state_dir``) и блок не берут.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_FILE = "robot.pid"


class RuntimeLockError(RuntimeError):
    """Боевой робот уже запущен в этом каталоге состояния."""


def _process_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID (без посылки ему сигналов)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill на Windows с любым сигналом завершает процесс — недопустимо.
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == still_active
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # чужой процесс — значит, существует
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire_runtime_lock(state_dir: Path) -> Path:
    """Занять PID-файл ``state_dir/robot.pid`` для боевого запуска.

    Возвращает путь к занятому файлу. Если файл занят живым процессом,
    поднимает :class:`RuntimeLockError` с сообщением для пользователя;
    файл с мёртвым или нечитаемым PID удаляется и попытка повторяется.
    """
    path = state_dir / LOCK_FILE
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o644)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and _process_alive(pid):
                raise RuntimeLockError(
                    f"Робот уже запущен (PID {pid}) в каталоге {state_dir}: "
                    "второй экземпляр в том же каталоге состояния запрещён."
                )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return path
    raise RuntimeLockError(
        f"Не удалось занять файл блокировки {path}: каталог занят другим запуском."
    )
