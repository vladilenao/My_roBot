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
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.assembly.contracts import contracts_from_payload, instrument_from_payload
from src.assembly.core_state import CLOSE_ALL, read_desired
from src.logging_setup import get_logger

log = get_logger(__name__)

STATUS_FILE = "status.json"
STATUS_SCHEMA_VERSION = 1
RUNNING = "running"
STOPPED = "stopped"
# Состояния участника состава (add-contour-membership).
WARMING, TRADING, DRAINING, CLOSING = "warming", "trading", "draining", "closing"


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
                 request_stop: Callable[[str], None], version: str = "", pid: int | None = None,
                 warm_timeout: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
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
        # Состав (add-contour-membership): ключ инструмента → состояние участника.
        self._roster = None
        self._members: dict[str, dict] = {}
        self._warm_timeout = warm_timeout
        self._clock = clock

    @property
    def state(self) -> str:
        return self._state

    @property
    def applied_generation(self) -> int:
        return self._generation

    @property
    def members(self) -> dict[str, str]:
        return {key: entry["state"] for key, entry in self._members.items()}

    def start(self, manager, roster=None) -> None:
        """Старт процесса: текущее желаемое применяется всегда — блок входов живёт в памяти менеджера."""
        self._manager = manager
        self._roster = roster
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
            self._apply_members(desired)
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
        if desired is not None:
            self._apply_members(desired)

    def after_tick(self) -> None:
        """Конец завершения тика: убранные без сделок выходят из состава; пустой остаток завершает остановку."""
        self._last_tick_at = _now()
        self._retire_drained()
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

    # ── Состав контура (add-contour-membership) ──

    def _apply_members(self, desired) -> None:
        """Желаемый состав против текущего: прогрев новых, возврат, снятие; только между тиками."""
        if self._roster is None or not desired.members:
            return
        present = self._roster.keys()
        for member in desired.members:
            entry = self._members.get(member.key)
            if entry is None and member.key in present:
                entry = self._members[member.key] = {"state": TRADING, "member": member}
            if member.active:
                self._activate(member, entry)
            elif entry is not None:
                self._deactivate(member, entry)
        self._progress_warming()

    def _activate(self, member, entry) -> None:
        if entry is None:
            log.info("Контур %s: %s добавлен — прогрев", self._contour_id, member.name)
            self._members[member.key] = {"state": WARMING, "member": member, "since": self._clock(), "warmed": False}
            return
        entry["member"] = member
        if entry["state"] in (DRAINING, CLOSING):
            log.info("Контур %s: %s возвращён в торговлю", self._contour_id, member.name)
            if self._manager is not None:
                self._manager.stop_instrument(member.key, None)
            entry["state"] = TRADING

    def _deactivate(self, member, entry) -> None:
        if entry["state"] == WARMING:
            log.info("Контур %s: %s убран до окончания прогрева", self._contour_id, member.name)
            self._members.pop(member.key)
            self._roster.retire(instrument_from_payload(member.instrument))
            return
        state = CLOSING if member.remove_mode == CLOSE_ALL else DRAINING
        if entry["state"] != state:
            log.info("Контур %s: %s убирается (%s)", self._contour_id, member.name, member.remove_mode)
        entry.update(state=state, member=member)
        if self._manager is not None:
            # Повтор каждый тик идемпотентен: вход мог исполниться до снятия, закрытие — не пройти.
            self._manager.stop_instrument(member.key, member.remove_mode)

    def _progress_warming(self) -> None:
        now = self._clock()
        for entry in self._members.values():
            if entry["state"] != WARMING:
                continue
            member = entry["member"]
            instrument = instrument_from_payload(member.instrument)
            if not entry["warmed"]:
                entry["warmed"] = self._roster.warm(instrument)
                continue
            expired = now - entry["since"] >= self._warm_timeout
            if not self._roster.ready(instrument) and not expired:
                continue
            if expired:
                log.warning("Контур %s: %s не прогрет за %.0f с — обзор с тем, что есть",
                            self._contour_id, member.name, self._warm_timeout)
            self._roster.admit(instrument, contracts_from_payload(member.contract or {}))
            entry["state"] = TRADING
            log.info("Контур %s: %s торгует", self._contour_id, member.name)

    def _retire_drained(self) -> None:
        for key, entry in list(self._members.items()):
            if entry["state"] not in (DRAINING, CLOSING):
                continue
            remainder = (0, 0) if self._manager is None else self._manager.contour_remainder(key)
            if remainder != (0, 0):
                continue
            instrument = self._roster.instrument(key) or instrument_from_payload(entry["member"].instrument)
            self._roster.retire(instrument)
            if self._manager is not None:
                self._manager.stop_instrument(key, None)
            self._members.pop(key)
            log.info("Контур %s: %s вышел из состава — сделок нет", self._contour_id, entry["member"].name)

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
                "members": [{"key": key, "name": entry["member"].name, "state": entry["state"]}
                            for key, entry in self._members.items()],
                "updated_at": _now(),
            })
        except OSError as exc:
            log.warning("status.json контура не записан (%s)", type(exc).__name__)
