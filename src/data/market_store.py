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

from src.scheduler.timing import tf_period_minutes

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
                CREATE TABLE IF NOT EXISTS recovery_attempts (
                    source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL, attempts INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
                    PRIMARY KEY(source,instrument_uid,interval,open_time_utc)
                );
                CREATE TABLE IF NOT EXISTS analysis_cursors (
                    source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                    assignment_id TEXT NOT NULL, open_time_utc TEXT NOT NULL,
                    PRIMARY KEY(source,instrument_uid,interval,assignment_id)
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

    @staticmethod
    def _live_key(kind, source, uid, interval="", open_time=None):
        identity = [source, uid, interval]
        if open_time is not None:
            identity.append(_utc_text(open_time))
        return "live:" + kind + ":" + json.dumps(identity, separators=(",", ":"))

    @staticmethod
    def _read_live(conn, key):
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _write_live(conn, key, value):
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, separators=(",", ":"), ensure_ascii=False)))

    def live_state(self, *, kind, source, instrument_uid, interval="", open_time=None):
        """Market-only факты; не меняют producer-id/schema/export контракт."""
        with self._lock, self._connect() as conn:
            return self._read_live(conn, self._live_key(kind, source, instrument_uid, interval, open_time))

    def save_live_state(self, value, *, kind, source, instrument_uid, interval="", open_time=None):
        with self._lock, self._connect() as conn:
            self._write_live(conn, self._live_key(kind, source, instrument_uid, interval, open_time), value)

    def accept_observation(self, observation, *, source=SOURCE_TBANK, reconcile_sequence=None):
        """Единое атомарное принятие нормализованного stream/RPC наблюдения.

        Различающийся поздний snapshot не выбирается по receipt-order. Его
        версия сохраняется, а current остаётся прежним до свежего RPC recheck
        с fence последней наблюдаемой версии. Preliminary не понижает closed.
        """
        return self.accept_observations([observation], source=source,
                                        reconcile_sequences=[reconcile_sequence])[0]

    def accept_observations(self, observations, *, source=SOURCE_TBANK, reconcile_sequences=None):
        """Пакет наблюдений одной страницы в одной транзакции.

        Каждое наблюдение проходит ту же процедуру, что и одиночное; пакет
        лишь избавляет историческую загрузку от транзакции на каждую свечу.
        """
        sequences = list(reconcile_sequences) if reconcile_sequences is not None else [None] * len(observations)
        if len(sequences) != len(observations):
            raise ValueError("Fence-последовательности не соответствуют наблюдениям")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                results = [self._accept_in(conn, item, source, sequence)
                           for item, sequence in zip(observations, sequences)]
                conn.commit()
                return results
            except BaseException:
                conn.rollback()
                raise

    def _accept_in(self, conn, observation, source, reconcile_sequence):
        uid, interval, stamp = observation.uid, observation.timeframe, _utc_text(observation.open_time)
        candle = self._normalise_candle(observation.as_row(), source, uid, interval, observation.received_at)
        identity = (source, uid, interval, stamp)
        facts_key = self._live_key("candle", source, uid, interval, stamp)
        uid_key = self._live_key("uid", source, uid)
        facts = self._read_live(conn, facts_key)
        old = conn.execute("SELECT * FROM candle_current WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                           identity).fetchone()
        provenance = {"transport": observation.transport, "epoch": observation.epoch,
            "received_at": candle["received_at"], "closure_evidence": observation.closure_evidence,
            "requested_source": observation.requested_source, "actual_source": observation.actual_source,
            "last_trade_time": None if observation.last_trade_time is None else _utc_text(observation.last_trade_time)}
        facts["last_observation"] = provenance
        if not observation.closed:
            facts["last_preliminary"] = {**provenance, "candle": candle}
            self._write_live(conn, facts_key, facts)
            return {"state": "preliminary", "revision": None, "changed": False}

        keys = ("open", "high", "low", "close", "volume", "is_complete")
        content = tuple(candle[key] for key in keys)
        same = old is not None and content == tuple(old[key] for key in keys)
        previous_source = facts.get("canonical_source")
        source_conflict = (previous_source not in (None, 0) and observation.actual_source not in (None, 0)
                           and previous_source != observation.actual_source)
        signature = [*content, observation.actual_source]
        fence_matches = (reconcile_sequence is not None and reconcile_sequence == facts.get("observed_sequence", 0)
                         and observation.transport == "rpc")
        if signature != facts.get("last_signature"):
            facts["observed_sequence"] = facts.get("observed_sequence", 0) + 1
            facts["last_signature"] = signature
        if same and not source_conflict:
            if fence_matches:
                facts.pop("conflict", None)
                facts["input_ready_at"] = candle["received_at"]
            facts.setdefault("observed_revision", old["revision"])
            facts.setdefault("published_at", old["received_at"])
            facts.setdefault("input_ready_at", old["received_at"])
            facts.setdefault("canonical_source", observation.actual_source)
            self._write_live(conn, facts_key, facts)
            return {"state": "conflict" if facts.get("conflict") else "accepted",
                    "revision": old["revision"], "changed": False}

        if signature == facts.get("observed_signature"):
            revision = facts["observed_revision"]
        else:
            revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM candle_versions WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                                    identity).fetchone()[0]
            payload = {**candle, "revision": revision}
            conn.execute("""INSERT INTO candle_versions(source,instrument_uid,interval,open_time_utc,revision,open,high,low,close,volume,is_complete,received_at)
                VALUES (:source,:instrument_uid,:interval,:open_time_utc,:revision,:open,:high,:low,:close,:volume,:is_complete,:received_at)""", payload)
            facts["observed_signature"] = signature
            facts["observed_revision"] = revision
        self._advance_cursor(conn, source, uid, interval, received=stamp)

        if old is not None and old["is_complete"] and not fence_matches:
            facts["conflict"] = {"candidate_revision": revision, "received_at": candle["received_at"],
                                 "reason": "source_conflict" if source_conflict else "values_conflict"}
            cursor = conn.execute("SELECT processed_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                                  (source, uid, interval)).fetchone()
            pin = facts.get("input_reference")
            consumed = (pin is not None and pin.get("state") == "consumed"
                        or cursor and cursor[0] is not None and stamp <= cursor[0])
            if interval == "1m" and consumed:
                held = self._read_live(conn, uid_key)
                held.update(integrity_hold=True, incident={"open_time": stamp, "candidate_revision": revision,
                    "consumed_revision": None if pin is None else pin["revision"],
                    "reason": "consumed_input_changed" if pin else "legacy_input_unconfirmed",
                    "received_at": candle["received_at"]})
                self._write_live(conn, uid_key, held)
            elif interval == "1m" and pin is not None:
                # Начатый пакет ещё не подтверждён финансовым owner.
                # Замораживаем зависимые новые действия, но не выдаём
                # сам факт pin за уже исполненную минуту.
                held = self._read_live(conn, uid_key)
                pending = held.setdefault("pending_input_conflicts", {})
                pending[stamp] = {"revision": pin["revision"], "candidate_revision": revision}
                self._write_live(conn, uid_key, held)
            self._write_live(conn, facts_key, facts)
            return {"state": "conflict", "revision": revision, "changed": False}

        payload = {**candle, "revision": revision}
        conn.execute("""INSERT INTO candle_current(source,instrument_uid,interval,open_time_utc,open,high,low,close,volume,is_complete,revision,received_at)
            VALUES (:source,:instrument_uid,:interval,:open_time_utc,:open,:high,:low,:close,:volume,:is_complete,:revision,:received_at)
            ON CONFLICT(source,instrument_uid,interval,open_time_utc) DO UPDATE SET
            open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
            is_complete=excluded.is_complete,revision=excluded.revision,received_at=excluded.received_at""", payload)
        self._upsert_coverage(conn, source, uid, interval, stamp, "received", observation.closure_evidence,
                              candle["received_at"], emit=False)
        self._append_change(conn, "candle_upsert", {"candle": self._export_candle(payload)})
        facts.pop("conflict", None)
        facts.update(published_at=candle["received_at"], input_ready_at=candle["received_at"],
                     canonical_source=observation.actual_source)
        self._write_live(conn, facts_key, facts)
        return {"state": "accepted", "revision": revision, "changed": True}

    def pin_input(self, *, source, instrument_uid, interval, open_time):
        """Закрепляет выбранную revision до внешней финансовой обработки."""
        key = self._live_key("candle", source, instrument_uid, interval, open_time)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                facts = self._read_live(conn, key)
                reference = facts.get("input_reference")
                if reference is None:
                    held = self._read_live(conn, self._live_key("uid", source, instrument_uid))
                    # Коррекция начатого, но не подтверждённого пакета
                    # замораживает новые зависимые минуты UID до commit.
                    if held.get("integrity_hold") or held.get("pending_input_conflicts") or facts.get("conflict"):
                        raise ValueError("Невозможно закрепить неподтверждённый рыночный вход")
                    row = conn.execute("SELECT revision,is_complete FROM candle_current WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                        (source, instrument_uid, interval, _utc_text(open_time))).fetchone()
                    if row is None or not row["is_complete"]:
                        raise ValueError("Нет завершённой версии рыночного входа")
                    reference = dict(source=source, instrument_uid=instrument_uid, interval=interval,
                                     open_time_utc=_utc_text(open_time), revision=row["revision"], state="started")
                    facts["input_reference"] = reference
                    self._write_live(conn, key, facts)
                conn.commit()
                return dict(reference)
            except BaseException:
                conn.rollback()
                raise

    def pinned_frame(self, reference):
        """Вход ранее начатого пакета, независимо от изменяемого current."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM candle_versions WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=? AND revision=?",
                tuple(reference[key] for key in ("source", "instrument_uid", "interval", "open_time_utc", "revision"))).fetchone()
        if row is None or not row["is_complete"]:
            raise ValueError("Закреплённая завершённая версия недоступна")
        return {"datetime": pd.Timestamp(row["open_time_utc"]).tz_convert("UTC").tz_localize(None),
                **{key: float(row[key]) for key in ("open", "high", "low", "close")},
                "volume": row["volume"], "is_complete": True}

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
                    if old is not None and old["is_complete"] and not candle["is_complete"]:
                        continue
                    revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM candle_versions WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                        (source, instrument_uid, interval, candle["open_time_utc"])).fetchone()[0]
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
                pair = self._read_live(conn, self._live_key("pair", source, instrument_uid, interval))
                cursor = conn.execute(
                    "SELECT received_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                    (source, instrument_uid, interval),
                ).fetchone()
                horizon = None if cursor is None or cursor[0] is None else max(stamp, cursor[0])
                # Граница может быть позже горизонта (cutover запуска): тогда
                # пересчитывать префикс нечего, запись состояния не падает.
                if pair.get("coverage_lower") and horizon is not None and horizon >= _utc_text(pair["coverage_lower"]):
                    self._resolve_contiguous_prefix(conn, source, instrument_uid, interval,
                                                    pair["coverage_lower"], horizon)
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

    def first_recovery_candidate(self, *, source: str, instrument_uid: str, interval: str,
                                 now=None, lower=None, upper=None) -> pd.Timestamp | None:
        """Самая старая дыра, которую нужно запросить отдельным backfill-ом."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT c.open_time_utc FROM interval_coverage c
                LEFT JOIN recovery_attempts r USING(source,instrument_uid,interval,open_time_utc)
                WHERE c.source=? AND c.instrument_uid=? AND c.interval=? AND c.state IN ('pending','unresolved')
                AND (? IS NULL OR c.open_time_utc>=?) AND (? IS NULL OR c.open_time_utc<=?)
                AND (? IS NULL OR r.next_attempt_at IS NULL OR r.next_attempt_at<=?)
                ORDER BY c.open_time_utc LIMIT 1""",
                (source, instrument_uid, interval,
                 None if lower is None else _utc_text(lower), None if lower is None else _utc_text(lower),
                 None if upper is None else _utc_text(upper), None if upper is None else _utc_text(upper),
                 None if now is None else _utc_text(now),
                 None if now is None else _utc_text(now)),
            ).fetchone()
        return None if row is None else pd.Timestamp(row["open_time_utc"]).tz_localize(None)

    def record_recovery_attempt(self, *, source, instrument_uid, interval, open_time, now, next_attempt):
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO recovery_attempts VALUES(?,?,?,?,1,?,?)
                ON CONFLICT(source,instrument_uid,interval,open_time_utc) DO UPDATE SET
                attempts=recovery_attempts.attempts+1,attempted_at=excluded.attempted_at,
                next_attempt_at=excluded.next_attempt_at""",
                (source, instrument_uid, interval, _utc_text(open_time), _utc_text(now), _utc_text(next_attempt)))

    def observations(self, *, source, instrument_uid, kind, limit=20):
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT payload_json,observed_at FROM source_observations
                WHERE source=? AND instrument_uid=? AND kind=? ORDER BY observation_id DESC LIMIT ?""",
                (source, instrument_uid, kind, limit)).fetchall()
        return [(json.loads(row[0]), pd.Timestamp(row[1]).tz_localize(None)) for row in rows]

    def analysis_cursor(self, *, source, instrument_uid, interval, assignment_id):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT open_time_utc FROM analysis_cursors WHERE source=? AND instrument_uid=? AND interval=? AND assignment_id=?",
                (source, instrument_uid, interval, assignment_id)).fetchone()
        return pd.Timestamp(row[0]).tz_localize(None) if row else None

    def mark_analyzed(self, *, source, instrument_uid, interval, assignment_id, open_time):
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO analysis_cursors VALUES(?,?,?,?,?)
                ON CONFLICT(source,instrument_uid,interval,assignment_id) DO UPDATE SET
                open_time_utc=MAX(analysis_cursors.open_time_utc,excluded.open_time_utc)""",
                (source, instrument_uid, interval, assignment_id, _utc_text(open_time)))

    def unresolved_between(self, *, source, instrument_uid, interval, start, end, limit=1000):
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT open_time_utc FROM interval_coverage WHERE
                source=? AND instrument_uid=? AND interval=? AND state IN ('pending','unresolved')
                AND open_time_utc>=? AND open_time_utc<? ORDER BY open_time_utc LIMIT ?""",
                (source, instrument_uid, interval, _utc_text(start), _utc_text(end), limit)).fetchall()
        return [pd.Timestamp(row[0]).tz_localize(None) for row in rows]

    def mark_processed(self, *, source: str, instrument_uid: str, interval: str, open_time: object) -> None:
        """Продвигает границу симуляции после внешнего durable commit."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stamp = _utc_text(open_time)
                key = self._live_key("candle", source, instrument_uid, interval, stamp)
                facts = self._read_live(conn, key)
                reference = facts.get("input_reference")
                if reference is not None:
                    reference["state"] = "consumed"
                    self._write_live(conn, key, facts)
                uid_key = self._live_key("uid", source, instrument_uid)
                held = self._read_live(conn, uid_key)
                pending = held.get("pending_input_conflicts", {})
                conflict = pending.pop(stamp, None)
                if conflict is not None:
                    held.update(integrity_hold=True, incident={"open_time": stamp,
                        "consumed_revision": conflict["revision"], "candidate_revision": conflict["candidate_revision"],
                        "reason": "consumed_input_changed", "received_at": _utc_text()})
                    self._write_live(conn, uid_key, held)
                self._advance_cursor(conn, source, instrument_uid, interval, processed=stamp)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def mark_resolved(self, *, source, instrument_uid, interval, open_time):
        """Computed coverage owner, отдельно от receipt/financial commit."""
        with self._lock, self._connect() as conn:
            self._advance_cursor(conn, source, instrument_uid, interval, resolved=_utc_text(open_time))

    def resolve_contiguous_prefix(self, *, source, instrument_uid, interval, meaningful_lower, horizon):
        """Продвигает coverage только по доказанно непрерывной последовательности.

        ``meaningful_lower`` задаёт владелец ingestion из прогрева и/или
        сохранённой execution boundary. Поэтому новый high-water никогда сам
        не пропускает старую внутреннюю или хвостовую дыру.
        """
        with self._lock, self._connect() as conn:
            resolved = self._resolve_contiguous_prefix(conn, source, instrument_uid, interval,
                                                       meaningful_lower, horizon)
        return None if resolved is None else pd.Timestamp(resolved).tz_localize(None)

    @staticmethod
    def _resolve_contiguous_prefix(conn, source, instrument_uid, interval, meaningful_lower, horizon):
        lower, upper = _utc_text(meaningful_lower), _utc_text(horizon)
        if upper < lower:
            raise ValueError("Горизонт coverage не может быть раньше meaningful lower")
        step = pd.Timedelta(minutes=tf_period_minutes(interval))
        current = pd.Timestamp(lower)
        if current.tzinfo is not None:
            current = current.tz_convert("UTC").tz_localize(None)
        end = pd.Timestamp(upper)
        if end.tzinfo is not None:
            end = end.tz_convert("UTC").tz_localize(None)
        resolved = None
        # Инкрементально: уже доказанный префикс не перепроверяется с lower.
        known = conn.execute("SELECT resolved_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                             (source, instrument_uid, interval)).fetchone()
        if known is not None and known[0] is not None and known[0] >= lower:
            resolved = known[0]
            current = pd.Timestamp(known[0]).tz_convert("UTC").tz_localize(None) + step
        while current <= end:
            row = conn.execute(
                "SELECT state FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                (source, instrument_uid, interval, _utc_text(current)),
            ).fetchone()
            if row is None or row["state"] not in RESOLVED_INTERVAL_STATES:
                break
            resolved = _utc_text(current)
            current += step
        if resolved is not None and (known is None or known[0] is None or resolved > known[0]):
            MarketDataStore._advance_cursor(conn, source, instrument_uid, interval, resolved=resolved)
        return resolved

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
