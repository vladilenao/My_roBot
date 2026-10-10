"""Долговечный журнал завершённых рыночных свечей.

Это хранилище принадлежит My Robot, а не торговому журналу.  Оно принимает
свечи от брокера, сохраняет каждую отличающуюся завершённую версию и выдаёт
append-only поток изменений для Historical Broker API Emulator.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

SCHEMA_VERSION = 2
SOURCE_TBANK = "tbank_exchange"
INTERVAL_STATES = frozenset({"received", "scheduled_closed", "confirmed_no_trade", "pending", "unresolved"})
RESOLVED_INTERVAL_STATES = frozenset({"received", "scheduled_closed", "confirmed_no_trade"})


def _utc_text(value: object | None = None) -> str:
    """ISO-8601 UTC без двусмысленности часового пояса."""
    if value is None:
        value = datetime.now(timezone.utc)
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat().replace("+00:00", "Z")


def _price_text(value: object) -> str:
    """Стабильное текстовое представление цены для SQLite и JSON."""
    return format(float(value), ".12g")


@dataclass(frozen=True)
class SourceInfo:
    producer_id: str
    schema_version: int
    min_after_id: int
    max_change_id: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "schema_version": self.schema_version,
            "min_after_id": self.min_after_id,
            "max_change_id": self.max_change_id,
        }


class MarketDataStore:
    """SQLite-хранилище независимое от памяти ``MarketDataCache``.

    Один экземпляр безопасен для потока получения данных и HTTP-потока. SQLite
    открывается на каждую короткую операцию: так сервер не держит транзакцию
    робота и корректно переживает его перезапуск.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _create_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candle_current (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL,
                    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
                    close TEXT NOT NULL, volume INTEGER NOT NULL,
                    is_complete INTEGER NOT NULL CHECK(is_complete IN (0, 1)),
                    revision INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
                );
                CREATE TABLE IF NOT EXISTS candle_versions (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
                    close TEXT NOT NULL, volume INTEGER NOT NULL,
                    is_complete INTEGER NOT NULL CHECK(is_complete IN (0, 1)),
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (source, instrument_uid, interval, open_time_utc, revision)
                );
                CREATE TABLE IF NOT EXISTS interval_coverage (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('received', 'scheduled_closed', 'confirmed_no_trade', 'pending', 'unresolved')),
                    evidence TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('candle_upsert', 'interval_status')),
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consumer_checkpoints (
                    consumer_id TEXT PRIMARY KEY,
                    producer_id TEXT NOT NULL,
                    acknowledged_through INTEGER NOT NULL,
                    acknowledged_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('trading_schedule', 'trading_status')),
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS source_observations_lookup
                    ON source_observations(source, instrument_uid, kind, observed_at DESC);
                CREATE TABLE IF NOT EXISTS market_cursors (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    received_through TEXT,
                    resolved_through TEXT,
                    processed_through TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source, instrument_uid, interval)
                );
                """
            )
            # SQLite не умеет добавить значение в CHECK. Ранняя версия change
            # уже могла создать таблицу без состояния ``received``; сохраняем
            # её строки при обновлении, а не требуем удалить локальную БД.
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='interval_coverage'"
            ).fetchone()[0]
            if "'received'" not in definition:
                conn.executescript(
                    """
                    ALTER TABLE interval_coverage RENAME TO interval_coverage_v1;
                    CREATE TABLE interval_coverage (
                        source TEXT NOT NULL,
                        instrument_uid TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        open_time_utc TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('received', 'scheduled_closed', 'confirmed_no_trade', 'pending', 'unresolved')),
                        evidence TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
                    );
                    INSERT INTO interval_coverage SELECT * FROM interval_coverage_v1;
                    DROP TABLE interval_coverage_v1;
                    """
                )
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._set_default(conn, "producer_id", str(uuid.uuid4()))
            self._set_default(conn, "min_after_id", "0")

    @staticmethod
    def _set_default(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)", (key, value))

    def source_info(self) -> SourceInfo:
        with self._lock, self._connect() as conn:
            values = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            maximum = conn.execute("SELECT COALESCE(MAX(change_id), 0) FROM changes").fetchone()[0]
        return SourceInfo(values["producer_id"], int(values["schema_version"]), int(values["min_after_id"]), maximum)

    @property
    def producer_id(self) -> str:
        return self.source_info().producer_id

    def record_candles(
        self,
        *,
        source: str,
        instrument_uid: str,
        interval: str,
        candles: pd.DataFrame | Iterable[dict[str, Any]],
        received_at: object | None = None,
    ) -> list[int]:
        """Атомарно фиксирует только новые завершённые версии.

        Предварительная свеча может быть записана в ``candle_current`` для
        диагностики, но не попадает в журнал экспорта. Идентичный повтор не
        меняет revision и не создаёт ``change_id``.
        """
        rows = candles.to_dict("records") if isinstance(candles, pd.DataFrame) else list(candles)
        changed: list[int] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    if "datetime" not in row:
                        raise ValueError("Свеча должна содержать datetime")
                    candle = self._normalise_candle(row, source, instrument_uid, interval, received_at)
                    old = conn.execute(
                        "SELECT * FROM candle_current WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                        (source, instrument_uid, interval, candle["open_time_utc"]),
                    ).fetchone()
                    content = tuple(candle[key] for key in ("open", "high", "low", "close", "volume", "is_complete"))
                    if old is not None and content == tuple(old[key] for key in ("open", "high", "low", "close", "volume", "is_complete")):
                        continue
                    revision = 1 if old is None else int(old["revision"]) + 1
                    payload = {**candle, "revision": revision}
                    conn.execute(
                        """INSERT INTO candle_current(source,instrument_uid,interval,open_time_utc,open,high,low,close,volume,is_complete,revision,received_at)
                        VALUES (:source,:instrument_uid,:interval,:open_time_utc,:open,:high,:low,:close,:volume,:is_complete,:revision,:received_at)
                        ON CONFLICT(source,instrument_uid,interval,open_time_utc) DO UPDATE SET
                          open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                          volume=excluded.volume, is_complete=excluded.is_complete, revision=excluded.revision,
                          received_at=excluded.received_at""",
                        payload,
                    )
                    conn.execute(
                        """INSERT INTO candle_versions(source,instrument_uid,interval,open_time_utc,revision,open,high,low,close,volume,is_complete,received_at)
                        VALUES (:source,:instrument_uid,:interval,:open_time_utc,:revision,:open,:high,:low,:close,:volume,:is_complete,:received_at)""",
                        payload,
                    )
                    if candle["is_complete"]:
                        self._upsert_coverage(
                            conn, source, instrument_uid, interval, candle["open_time_utc"],
                            "received", "completed_candle", candle["received_at"], emit=False,
                        )
                        self._advance_cursor(
                            conn, source, instrument_uid, interval,
                            received=candle["open_time_utc"],
                        )
                        change_id = self._append_change(conn, "candle_upsert", {"candle": self._export_candle(payload)})
                        changed.append(change_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return changed

    @staticmethod
    def _normalise_candle(row: dict[str, Any], source: str, instrument_uid: str, interval: str, received_at: object | None) -> dict[str, Any]:
        required = ("open", "high", "low", "close", "volume")
        missing = [name for name in required if name not in row]
        if missing:
            raise ValueError(f"В свече нет полей: {', '.join(missing)}")
        return {
            "source": source,
            "instrument_uid": str(instrument_uid),
            "interval": str(interval),
            "open_time_utc": _utc_text(row["datetime"]),
            "open": _price_text(row["open"]), "high": _price_text(row["high"]),
            "low": _price_text(row["low"]), "close": _price_text(row["close"]),
            "volume": int(row["volume"]),
            "is_complete": int(bool(row.get("is_complete", True))),
            "received_at": _utc_text(received_at),
        }

    @staticmethod
    def _export_candle(candle: dict[str, Any]) -> dict[str, Any]:
        return {**candle, "is_complete": bool(candle["is_complete"])}

    @staticmethod
    def _append_change(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> int:
        recorded_at = _utc_text()
        cursor = conn.execute(
            "INSERT INTO changes(kind, payload_json, recorded_at) VALUES (?, ?, ?)",
            (kind, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), recorded_at),
        )
        return int(cursor.lastrowid)

    def set_interval_state(self, *, source: str, instrument_uid: str, interval: str, open_time: object, state: str, evidence: str, observed_at: object | None = None) -> int | None:
        """Сохраняет доказанное состояние интервала и отдаёт его в экспорт.

        ``pending`` и ``unresolved`` намеренно не позволяют кэшу продвигать
        симуляцию; статус записывается отдельно от временной свечи.
        """
        if state not in INTERVAL_STATES:
            raise ValueError(f"Неизвестное состояние интервала: {state}")
        when = _utc_text(observed_at)
        stamp = _utc_text(open_time)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                change_id = self._upsert_coverage(conn, source, instrument_uid, interval, stamp, state, evidence, when, emit=True)
                conn.commit()
                return change_id
            except Exception:
                conn.rollback()
                raise

    def _upsert_coverage(self, conn: sqlite3.Connection, source: str, instrument_uid: str, interval: str, stamp: str, state: str, evidence: str, observed_at: str, *, emit: bool) -> int | None:
        old = conn.execute("SELECT state,evidence FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?", (source, instrument_uid, interval, stamp)).fetchone()
        if old is not None and old["state"] == state and old["evidence"] == evidence:
            return None
        conn.execute(
            """INSERT INTO interval_coverage(source,instrument_uid,interval,open_time_utc,state,evidence,observed_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(source,instrument_uid,interval,open_time_utc) DO UPDATE SET
            state=excluded.state,evidence=excluded.evidence,observed_at=excluded.observed_at""",
            (source, instrument_uid, interval, stamp, state, evidence, observed_at),
        )
        if not emit:
            return None
        status = {"source": source, "instrument_uid": instrument_uid, "interval": interval, "open_time_utc": stamp, "state": state, "evidence": evidence, "observed_at": observed_at}
        return self._append_change(conn, "interval_status", {"interval_status": status})

    def record_observation(
        self, *, source: str, instrument_uid: str, kind: str,
        payload: dict[str, Any], observed_at: object | None = None,
    ) -> None:
        """Сохраняет снимок расписания либо торгового статуса брокера.

        Наблюдение не доказывает отсутствие сделок: оно служит основанием для
        классификации ожидаемости минуты и для последующего аудита решения.
        """
        if kind not in {"trading_schedule", "trading_status"}:
            raise ValueError(f"Неизвестный тип наблюдения: {kind}")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO source_observations(source,instrument_uid,kind,payload_json,observed_at) VALUES (?,?,?,?,?)",
                (source, instrument_uid, kind, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), _utc_text(observed_at)),
            )

    def coverage_state(self, *, source: str, instrument_uid: str, interval: str, open_time: object) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                (source, instrument_uid, interval, _utc_text(open_time)),
            ).fetchone()
        return None if row is None else str(row["state"])

    def first_recovery_candidate(self, *, source: str, instrument_uid: str, interval: str) -> pd.Timestamp | None:
        """Самая старая дыра, которую нужно запросить отдельным backfill-ом."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT open_time_utc FROM interval_coverage
                WHERE source=? AND instrument_uid=? AND interval=? AND state IN ('pending','unresolved')
                ORDER BY open_time_utc LIMIT 1""",
                (source, instrument_uid, interval),
            ).fetchone()
        return None if row is None else pd.Timestamp(row["open_time_utc"]).tz_localize(None)

    def mark_processed(self, *, source: str, instrument_uid: str, interval: str, open_time: object) -> None:
        """Продвигает границу симуляции после внешнего durable commit."""
        with self._lock, self._connect() as conn:
            stamp = _utc_text(open_time)
            self._advance_cursor(
                conn, source, instrument_uid, interval, resolved=stamp, processed=stamp,
            )

    def cursors(self, *, source: str, instrument_uid: str, interval: str) -> dict[str, str | None]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT received_through,resolved_through,processed_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                (source, instrument_uid, interval),
            ).fetchone()
        return dict(row) if row is not None else {"received_through": None, "resolved_through": None, "processed_through": None}

    @staticmethod
    def _advance_cursor(
        conn: sqlite3.Connection, source: str, instrument_uid: str, interval: str,
        *, received: str | None = None, resolved: str | None = None, processed: str | None = None,
    ) -> None:
        now = _utc_text()
        conn.execute(
            """INSERT INTO market_cursors(source,instrument_uid,interval,received_through,resolved_through,processed_through,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,instrument_uid,interval) DO UPDATE SET
              received_through=CASE WHEN excluded.received_through IS NULL THEN market_cursors.received_through
                WHEN market_cursors.received_through IS NULL OR excluded.received_through > market_cursors.received_through THEN excluded.received_through ELSE market_cursors.received_through END,
              resolved_through=CASE WHEN excluded.resolved_through IS NULL THEN market_cursors.resolved_through
                WHEN market_cursors.resolved_through IS NULL OR excluded.resolved_through > market_cursors.resolved_through THEN excluded.resolved_through ELSE market_cursors.resolved_through END,
              processed_through=CASE WHEN excluded.processed_through IS NULL THEN market_cursors.processed_through
                WHEN market_cursors.processed_through IS NULL OR excluded.processed_through > market_cursors.processed_through THEN excluded.processed_through ELSE market_cursors.processed_through END,
              updated_at=excluded.updated_at""",
            (source, instrument_uid, interval, received, resolved, processed, now),
        )

    def changes_after(self, producer_id: str, after_id: int, limit: int = 500) -> dict[str, Any]:
        info = self.source_info()
        if producer_id != info.producer_id:
            raise ProducerMismatch()
        if after_id < info.min_after_id:
            raise CursorExpired()
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT change_id,kind,payload_json,recorded_at FROM changes WHERE change_id>? ORDER BY change_id LIMIT ?", (after_id, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        result = []
        for row in rows:
            result.append({"change_id": row["change_id"], "kind": row["kind"], "recorded_at": row["recorded_at"], **json.loads(row["payload_json"])})
        return {"producer_id": info.producer_id, "next_after_id": result[-1]["change_id"] if result else after_id, "has_more": has_more, "changes": result}

    def acknowledge(self, consumer_id: str, producer_id: str, change_id: int) -> dict[str, Any]:
        if not consumer_id or not consumer_id.replace("-", "").isalnum() or consumer_id.lower() != consumer_id:
            raise ValueError("consumer_id должен содержать строчные латинские буквы, цифры и дефисы")
        info = self.source_info()
        if producer_id != info.producer_id:
            raise ProducerMismatch()
        if change_id < 0 or change_id > info.max_change_id:
            raise ValueError("change_id вне доступного диапазона")
        now = _utc_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT acknowledged_through FROM consumer_checkpoints WHERE consumer_id=?", (consumer_id,)).fetchone()
            acknowledged = max(change_id, int(old[0])) if old else change_id
            conn.execute("""INSERT INTO consumer_checkpoints(consumer_id,producer_id,acknowledged_through,acknowledged_at)
                VALUES(?,?,?,?) ON CONFLICT(consumer_id) DO UPDATE SET
                producer_id=excluded.producer_id,acknowledged_through=MAX(consumer_checkpoints.acknowledged_through,excluded.acknowledged_through),acknowledged_at=excluded.acknowledged_at""", (consumer_id, producer_id, acknowledged, now))
            conn.commit()
        return {"producer_id": info.producer_id, "consumer_id": consumer_id, "acknowledged_through": acknowledged}

    def current_frame(self, *, source: str, instrument_uid: str, interval: str) -> pd.DataFrame:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT open_time_utc,open,high,low,close,volume,is_complete FROM candle_current WHERE source=? AND instrument_uid=? AND interval=? ORDER BY open_time_utc", (source, instrument_uid, interval)).fetchall()
        if not rows:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "is_complete"])
        frame = pd.DataFrame([dict(row) for row in rows]).rename(columns={"open_time_utc": "datetime"})
        frame["datetime"] = pd.to_datetime(frame["datetime"], utc=True).dt.tz_localize(None)
        for key in ("open", "high", "low", "close"):
            frame[key] = frame[key].astype(float)
        frame["is_complete"] = frame["is_complete"].astype(bool)
        return frame


class ProducerMismatch(Exception):
    """Курсор принадлежит другой базе источника."""


class CursorExpired(Exception):
    """Потребитель запросил удалённую часть журнала."""
