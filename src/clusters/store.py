"""Хранилище событий ClusterSource: ``cluster-events.sqlite3``.

Внешние факты, а не торговое состояние: ``trades.sqlite3`` остаётся
единственным источником истины по сделкам. Запись идемпотентна по
``event_id`` — повтор доставки из Redis не создаёт второй строки.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA_VERSION = 1
FILE_NAME = "cluster-events.sqlite3"
SIGNAL_TYPES = frozenset({"large_aggressor", "volume_spike", "stacked_imbalance", "absorption"})


@dataclass(frozen=True)
class StoredEvent:
    """Событие ClusterSource в разобранном виде."""

    event_id: str
    stream_id: str
    category: str
    event_type: str
    secid: str
    source_ticker: str
    direction: str | None
    signal_id: str | None
    timestamp: datetime
    published_at: datetime
    payload: Mapping[str, object]
    fields: Mapping[str, str]


def utc_text(value: datetime) -> str:
    """Единый вид времени в базе: UTC с микросекундами — сравнивается как строка."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(value: str) -> datetime:
    """Время ISO 8601 из потока или базы → aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def keep(fields: Mapping[str, str]) -> bool:
    """Что сохраняется: сигналы, качество данных и отзывы; подтверждения — нет."""
    category = fields.get("category")
    return category in {"signal", "data_quality"} or fields.get("event_type") == "signal_revoked"


class _Missing(Exception):
    """Хранилища ещё нет: процесс контура читает его до первой записи ядром."""


def _or_empty(default):
    """Чтение без файла (только чтение, ядро ещё не создало хранилище) — пустой ответ."""

    def wrap(method):
        def read(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except _Missing:
                return default() if callable(default) else default
        read.__name__, read.__doc__ = method.__name__, method.__doc__
        return read
    return wrap


class ClusterEventStore:
    """SQLite с событиями потока; соединение на вызов, WAL, общий lock записи."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # Процесс контура только читает хранилище ядра (add-contour-process, D7).
        self.read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"{self.path.name}: неизвестная версия схемы {version}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cluster_events (
                    event_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    secid TEXT NOT NULL,
                    source_ticker TEXT NOT NULL,
                    direction TEXT,
                    signal_id TEXT,
                    timestamp TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_events_secid ON cluster_events (secid, published_at);
                CREATE INDEX IF NOT EXISTS cluster_events_signal ON cluster_events (signal_id);
                CREATE INDEX IF NOT EXISTS cluster_events_published ON cluster_events (published_at);
                CREATE TABLE IF NOT EXISTS consumer_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            if not self.path.exists():
                raise _Missing()
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ── запись ──
    def write(self, entries: Iterable[tuple[str, Mapping[str, str]]], *, received_at: datetime | None = None) -> int:
        """Сохраняет пачку ``(stream_id, поля)`` одной транзакцией; возвращает число новых строк.

        Поля, не относящиеся к хранению (``keep``), пропускаются. Ошибка разбора
        одной записи не теряет пачку: такая запись пропускается.
        """
        received = utc_text(received_at or datetime.now(timezone.utc))
        rows = []
        last_stream_id = None
        for stream_id, fields in entries:
            last_stream_id = stream_id
            if not keep(fields):
                continue
            try:
                rows.append((
                    fields["event_id"], stream_id, fields["category"], fields["event_type"],
                    fields["secid"], fields.get("ticker", ""), fields.get("direction"),
                    fields.get("signal_id"), utc_text(parse_utc(fields["timestamp"])),
                    utc_text(parse_utc(fields["published_at"])),
                    json.dumps(dict(fields), ensure_ascii=False, sort_keys=True), received,
                ))
            except (KeyError, ValueError):
                continue
        with self._lock, self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO cluster_events (event_id, stream_id, category, event_type, secid, "
                "source_ticker, direction, signal_id, timestamp, published_at, fields_json, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            inserted = conn.total_changes - before
            if last_stream_id is not None:
                conn.execute(
                    "INSERT INTO consumer_state (key, value) VALUES ('last_stream_id', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                    (last_stream_id,),
                )
        return inserted

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO consumer_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    @_or_empty(None)
    def state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM consumer_state WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    # ── чтение ──
    @_or_empty(list)
    def signals(self, secid: str, *, after: datetime, until: datetime) -> list[StoredEvent]:
        """Сигналы инструмента с ``after < published_at ≤ until`` по возрастанию времени."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cluster_events WHERE secid=? AND category='signal' "
                "AND published_at>? AND published_at<=? ORDER BY published_at, event_id",
                (secid, utc_text(after), utc_text(until)),
            ).fetchall()
        return [_event(row) for row in rows]

    @_or_empty(set)
    def revoked(self, signal_ids: Iterable[str], *, until: datetime) -> set[str]:
        """Какие из ``signal_ids`` отозваны с ``published_at ≤ until``."""
        ids = sorted(set(signal_ids))
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT signal_id FROM cluster_events WHERE event_type='signal_revoked' "
                f"AND published_at<=? AND signal_id IN ({marks})",
                (utc_text(until), *ids),
            ).fetchall()
        return {str(row["signal_id"]) for row in rows}

    @_or_empty(None)
    def last_quality(self, secid: str, *, until: datetime) -> str | None:
        """Тип последнего события качества данных инструмента до ``until``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_type FROM cluster_events WHERE secid=? AND category='data_quality' "
                "AND published_at<=? ORDER BY published_at DESC, event_id DESC LIMIT 1",
                (secid, utc_text(until)),
            ).fetchone()
        return None if row is None else str(row["event_type"])

    @_or_empty(False)
    def has_events(self, secid: str, *, until: datetime) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM cluster_events WHERE secid=? AND published_at<=? LIMIT 1",
                (secid, utc_text(until)),
            ).fetchone()
        return row is not None

    @_or_empty(set)
    def secids(self, *, after: datetime, until: datetime) -> set[str]:
        """Инструменты, по которым были события в окне — для поиска другого контракта семейства."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT secid FROM cluster_events WHERE published_at>? AND published_at<=?",
                (utc_text(after), utc_text(until)),
            ).fetchall()
        return {str(row["secid"]) for row in rows}

    @_or_empty(None)
    def event(self, event_id: str) -> StoredEvent | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cluster_events WHERE event_id=?", (event_id,)).fetchone()
        return None if row is None else _event(row)


def copy_store(source: Path, target: Path) -> bool:
    """Копия боевого хранилища для исторического прогона; источник открывается только на чтение."""
    if not source.is_file() or target.exists():
        return False
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)) as src_conn, \
                closing(sqlite3.connect(target)) as dst_conn:
            src_conn.backup(dst_conn)
    except sqlite3.Error:
        target.unlink(missing_ok=True)
        return False
    return True


def _event(row: sqlite3.Row) -> StoredEvent:
    fields = json.loads(row["fields_json"])
    try:
        payload = json.loads(fields.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return StoredEvent(
        event_id=row["event_id"], stream_id=row["stream_id"], category=row["category"],
        event_type=row["event_type"], secid=row["secid"], source_ticker=row["source_ticker"],
        direction=row["direction"], signal_id=row["signal_id"],
        timestamp=parse_utc(row["timestamp"]), published_at=parse_utc(row["published_at"]),
        payload=payload if isinstance(payload, dict) else {}, fields=fields,
    )
