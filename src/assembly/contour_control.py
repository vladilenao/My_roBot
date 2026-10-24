"""Сторона процесса контура: желаемое из базы ядра, остановка на границе тика, ``status.json`` (add-control-api).

Контур читает ``core.sqlite3`` только на чтение в начале завершения каждого
тика и применяет разницу через менеджер сделок. Применённое поколение и
фактическое состояние он хранит в таблице ``contour_state`` своей базы
``trades.sqlite3``, а снимок для API ядра — в ``status.json`` своей папки.
Когда остановка применена и сделок не осталось, контур просит завершить цикл
после текущего тика. Модуль не импортирует ядро.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.assembly.core_state import read_desired
from src.logging_setup import get_logger

log = get_logger(__name__)

STATUS_FILE = "status.json"
STATUS_SCHEMA_VERSION = 1
RUNNING = "running"
STOPPED = "stopped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_status(path: Path, payload: dict) -> None:
    """Атомарная запись снимка: временный файл рядом и переименование."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"schema_version": STATUS_SCHEMA_VERSION, **payload}, ensure_ascii=False),
                         encoding="utf-8")
    os.replace(temporary, path)


def read_status(path: Path) -> dict | None:
    """Снимок контура для ядра; нет файла, он повреждён или другой версии — ``None``."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != STATUS_SCHEMA_VERSION:
        return None
    return payload


class ContourControl:
    """Применение желаемого состояния ядра внутри процесса контура."""

    def __init__(self, *, core_db: Path, contour_id: str, journal_db: Path, status_path: Path,
                 request_stop: Callable[[str], None], version: str = "", pid: int | None = None) -> None:
        self._core_db = Path(core_db)
        self._contour_id = contour_id
        self._journal_db = Path(journal_db)
        self._status_path = Path(status_path)
        self._request_stop = request_stop
        self._version = version
        self._pid = pid or os.getpid()
        self._manager = None
        self._generation = 0
        self._mode: str | None = None
        self._state = RUNNING
        self._remainder = (0, 0)
        self._last_tick_at: str | None = None
        self._read_failed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def applied_generation(self) -> int:
        return self._generation

    def start(self, manager) -> None:
        """Старт процесса: текущее желаемое применяется всегда — блок входов живёт в памяти менеджера."""
        self._manager = manager
        with closing(self._journal()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS contour_state (
                contour_id TEXT PRIMARY KEY, applied_generation INTEGER NOT NULL, state TEXT NOT NULL,
                stop_mode TEXT, updated_at TEXT NOT NULL)""")
            row = conn.execute("SELECT applied_generation FROM contour_state WHERE contour_id=?",
                               (self._contour_id,)).fetchone()
        if row is not None:
            self._generation = int(row[0])
        desired = self._read()
        if desired is not None:
            self._apply(desired)
        self._remainder = self._count()
        self._write_status()

    def before_tick(self) -> None:
        """Начало завершения тика: новое поколение применяется, остановка повторяется до пустого остатка."""
        desired = self._read()
        if desired is not None and desired.generation != self._generation:
            self._apply(desired)
        elif self._mode is not None and self._manager is not None:
            # Вход мог исполниться до снятия, а закрытие — не пройти: повтор идемпотентен.
            self._manager.stop_contour(self._mode)

    def after_tick(self) -> None:
        """Конец завершения тика: при пустом остатке остановка завершается, затем снимок."""
        self._last_tick_at = _now()
        self._remainder = self._count()
        if self._mode is not None and self._state != STOPPED and self._remainder == (0, 0):
            self._state = STOPPED
            self._save()
            log.info("Контур %s: остановка командой выполнена, сделок нет — завершение", self._contour_id)
            self._request_stop("остановка командой ядра")
        self._write_status()

    def close(self) -> None:
        self._write_status()

    def _apply(self, desired) -> None:
        mode = None if desired.running else desired.stop_mode
        if mode != self._mode:
            log.info("Контур %s: поколение %d — %s", self._contour_id, desired.generation,
                     "торговля" if mode is None else f"остановка {mode}")
        self._mode = mode
        if self._manager is not None:
            self._manager.stop_contour(mode)
        self._state = RUNNING if mode is None else f"stopping_{mode}"
        self._generation = desired.generation
        self._save()

    def _read(self):
        try:
            desired = read_desired(self._core_db, self._contour_id)
        except sqlite3.Error as exc:
            if not self._read_failed:
                log.warning("База ядра не читается (%s): контур продолжает с поколением %d",
                            type(exc).__name__, self._generation)
            self._read_failed = True
            return None
        self._read_failed = False
        return desired

    def _count(self) -> tuple[int, int]:
        return (0, 0) if self._manager is None else self._manager.contour_remainder()

    def _journal(self) -> sqlite3.Connection:
        return sqlite3.connect(self._journal_db, timeout=10)

    def _save(self) -> None:
        with closing(self._journal()) as conn, conn:
            conn.execute("""INSERT INTO contour_state(contour_id,applied_generation,state,stop_mode,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(contour_id) DO UPDATE SET applied_generation=excluded.applied_generation,
                state=excluded.state, stop_mode=excluded.stop_mode, updated_at=excluded.updated_at""",
                         (self._contour_id, self._generation, self._state, self._mode, _now()))

    def _write_status(self) -> None:
        positions, entries = self._remainder
        try:
            write_status(self._status_path, {
                "contour_id": self._contour_id, "state": self._state, "applied_generation": self._generation,
                "stop_mode": self._mode, "open_positions": positions, "pending_entries": entries,
                "last_tick_at": self._last_tick_at, "version": self._version, "pid": self._pid,
                "updated_at": _now(),
            })
        except OSError as exc:
            log.warning("status.json контура не записан (%s)", type(exc).__name__)
