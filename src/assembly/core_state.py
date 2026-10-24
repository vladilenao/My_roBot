"""Желаемое состояние контуров и журнал команд ядра — ``data/core.sqlite3`` (add-control-api).

Пишет только ядро; процесс контура открывает базу только на чтение
(``read_desired``). Каждая команда, меняющая желаемое, увеличивает номер
поколения: контур применяет его на границе тика и сообщает применённое
поколение в своём ``status.json``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CORE_DATABASE_FILE = "core.sqlite3"
SCHEMA_VERSION = 1
NO_ENTRIES = "no_entries"
CLOSE_ALL = "close_all"
STOP_MODES = (NO_ENTRIES, CLOSE_ALL)

# Исходы команд в журнале.
ACCEPTED, REJECTED, CONFLICT, IGNORED = "accepted", "rejected", "conflict", "ignored"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Desired:
    """Желаемое состояние одного контура."""

    contour_id: str
    kind: str
    running: bool
    stop_mode: str | None
    generation: int
    instruments: tuple = ()
    source: str | None = None
    command: str | None = None
    updated_at: str | None = None

    def payload(self) -> dict:
        return {"running": self.running, "stop_mode": self.stop_mode, "generation": self.generation}


@dataclass(frozen=True)
class StopOutcome:
    """Итог команды остановки: ``accepted`` — новое поколение, ``ignored`` — повтор, ``conflict`` — отказ."""

    outcome: str
    generation: int
    reason: str = ""


def _desired(row) -> Desired | None:
    if row is None:
        return None
    return Desired(row["contour_id"], row["kind"], row["desired"] == "running", row["stop_mode"],
                   int(row["generation"]), tuple(json.loads(row["instruments_json"])), row["source"],
                   row["command"], row["updated_at"])


def read_desired(path: Path, contour_id: str) -> Desired | None:
    """Сторона контура: желаемое из базы ядра только на чтение; нет базы или строки — ``None``."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        return _desired(conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())


class CoreStateStore:
    """База ядра: желаемое контуров с поколениями и журнал команд управления."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contours (
                    contour_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    desired TEXT NOT NULL CHECK(desired IN ('running', 'stopped')),
                    stop_mode TEXT CHECK(stop_mode IN ('no_entries', 'close_all')),
                    generation INTEGER NOT NULL,
                    instruments_json TEXT NOT NULL,
                    source TEXT,
                    command TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contour_commands (
                    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contour_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generation INTEGER,
                    outcome TEXT NOT NULL CHECK(outcome IN ('accepted', 'rejected', 'conflict', 'ignored')),
                    message TEXT
                );
            """)
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))

    @contextmanager
    def _connect(self):
        """Короткое соединение на операцию; транзакции — явным ``BEGIN IMMEDIATE``."""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0])

    def desired(self, contour_id: str) -> Desired | None:
        with self._connect() as conn:
            return _desired(conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())

    def record_launch(self, contour_id: str, kind: str, instruments, *, source: str = "", command: str = "",
                      payload: dict | None = None) -> int:
        """Запуск: желаемое «работает» новым поколением и выбранный состав; возвращает поколение."""
        instruments = list(instruments)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                generation = self._generation(conn, contour_id) + 1
                now = _now()
                conn.execute("""INSERT INTO contours(contour_id,kind,desired,stop_mode,generation,instruments_json,
                    source,command,updated_at) VALUES(?,?,'running',NULL,?,?,?,?,?)
                    ON CONFLICT(contour_id) DO UPDATE SET kind=excluded.kind, desired='running', stop_mode=NULL,
                    generation=excluded.generation, instruments_json=excluded.instruments_json,
                    source=excluded.source, command=excluded.command, updated_at=excluded.updated_at""",
                    (contour_id, kind, generation, json.dumps(instruments, ensure_ascii=False), source, command, now))
                self._log(conn, contour_id, "launch", payload if payload is not None else {"instruments": instruments},
                          generation, ACCEPTED, None, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return generation

    def record_stop(self, contour_id: str, mode: str) -> StopOutcome:
        """Остановка: новое поколение; повтор того же режима — ``ignored``; ``close_all`` → ``no_entries`` — отказ."""
        if mode not in STOP_MODES:
            raise ValueError(f"Неизвестный режим остановки: {mode!r}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = _desired(conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())
                now = _now()
                if current is None:
                    outcome = StopOutcome(CONFLICT, 0, "контур ещё не запускался")
                elif not current.running and current.stop_mode == mode:
                    outcome = StopOutcome(IGNORED, current.generation)
                elif not current.running and current.stop_mode == CLOSE_ALL:
                    outcome = StopOutcome(CONFLICT, current.generation,
                                          "контур уже закрывает все сделки, режим без входов не вернуть")
                else:
                    outcome = StopOutcome(ACCEPTED, current.generation + 1)
                    conn.execute("UPDATE contours SET desired='stopped', stop_mode=?, generation=?, updated_at=? "
                                 "WHERE contour_id=?", (mode, outcome.generation, now, contour_id))
                self._log(conn, contour_id, "stop", {"mode": mode}, outcome.generation or None, outcome.outcome,
                          outcome.reason or None, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return outcome

    def record_command(self, contour_id: str, action: str, payload: dict, outcome: str, message: str = "") -> None:
        """Команда без смены желаемого: отклонённый запуск, конфликт, отказ остановки."""
        with self._lock, self._connect() as conn:
            current = conn.execute("SELECT generation FROM contours WHERE contour_id=?", (contour_id,)).fetchone()
            self._log(conn, contour_id, action, payload, None if current is None else int(current[0]),
                      outcome, message or None, _now())

    def commands(self, contour_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM contour_commands" + (" WHERE contour_id=?" if contour_id else "")
        args = (contour_id,) if contour_id else ()
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY command_id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    @staticmethod
    def _generation(conn, contour_id) -> int:
        row = conn.execute("SELECT generation FROM contours WHERE contour_id=?", (contour_id,)).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _log(conn, contour_id, action, payload, generation, outcome, message, at) -> None:
        conn.execute("""INSERT INTO contour_commands(contour_id,received_at,action,payload_json,generation,outcome,message)
            VALUES(?,?,?,?,?,?,?)""", (contour_id, at, action, json.dumps(payload, ensure_ascii=False, default=str),
                                       generation, outcome, message))
