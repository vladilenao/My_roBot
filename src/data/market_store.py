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
from src.market_calendar.serialization import canonical_day_json, day_version
from src.market_calendar.intervals import normalize_exchange, trading_date

SCHEMA_VERSION = 4
SOURCE_TBANK = "tbank_exchange"
from src.data.coverage_states import (  # noqa: E402,F401
    EXPORTED_STATE, FINAL_EVIDENCE_STATES, INTERVAL_STATES, RESOLVED_INTERVAL_STATES, WAITING_INTERVAL_STATES,
)
ROSTER_ORIGINS = frozenset({"selected", "obligation", "previous"})
# Подтверждения ленты расписаний живут в общей таблице под этим префиксом.
SCHEDULE_CHECKPOINT_PREFIX = "trading-schedules:"
# Курсоры ленты торговых контуров: внешним потребителям этот префикс закрыт.
CONTOUR_CONSUMER_PREFIX = "contour-"
# Отметки исполнения, записанные до add-contour-market-feed, принадлежат
# единственному тогда исполнителю в живой базе — эмуляции.
LEGACY_CONTOUR = "emulation"
# Порция очистки ленты /changes — одна короткая транзакция записи.
PRUNE_BATCH = 5000


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


def _downgrades_final(current: str, state: str) -> bool:
    """Ожидание поверх итога по данным источника — понижение, его не пишем."""
    return current in FINAL_EVIDENCE_STATES and state in WAITING_INTERVAL_STATES


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


@dataclass(frozen=True)
class ChangesPrune:
    """Итог одной порции очистки ленты ``/changes``.

    ``more`` — в начале ленты остались строки, которые можно удалить
    следующей порцией; ``holder`` — потребитель, чьё подтверждение
    остановило порцию, ``holder_lag`` — сколько записей он не дочитал.
    """

    deleted: int
    min_after_id: int
    more: bool
    holder: str | None = None
    holder_lag: int = 0


def _require_contour(contour: str | None) -> None:
    """Отметку исполнения пишет только торговый контур под своим ключом."""
    if not contour or not isinstance(contour, str):
        raise ValueError("Отметка исполнения требует идентификатор торгового контура")


def _require_broker(provenance: str) -> None:
    if provenance != "broker":
        raise ValueError(f"Расписание происхождения {provenance!r} не сохраняется в рыночной базе")


@dataclass(frozen=True)
class StoredScheduleDay:
    source: str
    exchange: str
    trading_date: str
    version: str
    raw_json: str
    is_trading_day: bool
    observed_at: str
    last_confirmed_at: str
    schedule_change_id: int
    # Происхождение дня: ``broker`` — наблюдён у брокера; ``reconstructed`` —
    # восстановлен историческим источником и живёт только в снимке прогона.
    provenance: str = "broker"
    rules_version: str | None = None


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
                    state TEXT NOT NULL CHECK(state IN ('received', 'scheduled_closed', 'confirmed_no_trade', 'source_absent', 'pending', 'unresolved')),
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
                CREATE TABLE IF NOT EXISTS contour_cursors (
                    contour_id TEXT NOT NULL,
                    source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                    processed_through TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(contour_id,source,instrument_uid,interval)
                );
                CREATE TABLE IF NOT EXISTS contour_demand (
                    contour_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL, interval TEXT NOT NULL,
                    min_bars INTEGER NOT NULL,
                    instrument_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(contour_id,instrument_key,interval)
                );
                CREATE TABLE IF NOT EXISTS contour_obligations (
                    contour_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    lower_open_time TEXT NOT NULL,
                    inclusive INTEGER NOT NULL CHECK(inclusive IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(contour_id,instrument_key)
                );
                CREATE TABLE IF NOT EXISTS pair_readiness (
                    source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                    status_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source,instrument_uid,interval)
                );
                CREATE TABLE IF NOT EXISTS trading_schedule_days (
                    source TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    version TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    is_trading_day INTEGER NOT NULL CHECK(is_trading_day IN (0, 1)),
                    observed_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    schedule_change_id INTEGER NOT NULL UNIQUE,
                    PRIMARY KEY (source, exchange, trading_date, version)
                );
                CREATE INDEX IF NOT EXISTS trading_schedule_days_latest
                    ON trading_schedule_days(source, exchange, trading_date, schedule_change_id DESC);
                CREATE TABLE IF NOT EXISTS instrument_schedule_bindings (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    schedule_change_id INTEGER NOT NULL UNIQUE,
                    PRIMARY KEY (source, instrument_uid, observed_at)
                );
                CREATE INDEX IF NOT EXISTS instrument_schedule_bindings_latest
                    ON instrument_schedule_bindings(source, instrument_uid, observed_at DESC);
                CREATE TABLE IF NOT EXISTS market_data_roster (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    short_name TEXT,
                    origin TEXT NOT NULL CHECK(origin IN ('selected', 'obligation', 'previous')),
                    window_lower_utc TEXT,
                    requirements_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    PRIMARY KEY (source, instrument_uid, interval)
                );
                CREATE TABLE IF NOT EXISTS interval_checks (
                    source TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL,
                    checks INTEGER NOT NULL,
                    first_checked_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    next_check_at TEXT,
                    PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
                );
                CREATE INDEX IF NOT EXISTS interval_checks_due
                    ON interval_checks(source, instrument_uid, interval, next_check_at);
                CREATE TABLE IF NOT EXISTS pattern_event_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS pattern_event_outbox_due
                    ON pattern_event_outbox(delivered_at, next_attempt_at);
                """
            )
            # SQLite не умеет добавить значение в CHECK. Ранняя версия change
            # уже могла создать таблицу без состояния ``received``; сохраняем
            # её строки при обновлении, а не требуем удалить локальную БД.
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='interval_coverage'"
            ).fetchone()[0]
            if "'source_absent'" not in definition:
                self._backup_before_migration(conn, "interval_coverage")
                conn.executescript(
                    """
                    ALTER TABLE interval_coverage RENAME TO interval_coverage_v1;
                    CREATE TABLE interval_coverage (
                        source TEXT NOT NULL,
                        instrument_uid TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        open_time_utc TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('received', 'scheduled_closed', 'confirmed_no_trade', 'source_absent', 'pending', 'unresolved')),
                        evidence TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
                    );
                    INSERT INTO interval_coverage SELECT * FROM interval_coverage_v1;
                    DROP TABLE interval_coverage_v1;
                    """
                )
            self._migrate_contour_marks(conn)
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._set_default(conn, "producer_id", str(uuid.uuid4()))
            self._set_default(conn, "min_after_id", "0")
            self._set_default(conn, "schedule_max_change_id", "0")

    def _migrate_contour_marks(self, conn: sqlite3.Connection) -> None:
        """Отметки исполнения переходят под ключ контура (add-contour-market-feed).

        Курсоры анализа получают ``contour_id`` (пересоздание таблицы),
        граница обработки уходит из ``market_cursors`` в ``contour_cursors``,
        закрепление входа — в ``input_references``. Всё прежнее принадлежит
        эмуляции. Версия схемы экспорта не меняется: контракт ``/changes`` тот же.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis_cursors)").fetchall()}
        migrated = conn.execute("SELECT 1 FROM metadata WHERE key='contour_marks'").fetchone() is not None
        if "contour_id" in columns and migrated:
            return
        legacy = bool(columns) and "contour_id" not in columns
        if legacy or conn.execute("SELECT 1 FROM market_cursors WHERE processed_through IS NOT NULL LIMIT 1").fetchone():
            self._backup_before_migration(conn, "market_cursors", label="pre-contour-feed")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if legacy:
                conn.execute("ALTER TABLE analysis_cursors RENAME TO analysis_cursors_v1")
            conn.execute("""CREATE TABLE IF NOT EXISTS analysis_cursors (
                contour_id TEXT NOT NULL,
                source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                assignment_id TEXT NOT NULL, open_time_utc TEXT NOT NULL,
                PRIMARY KEY(contour_id,source,instrument_uid,interval,assignment_id))""")
            if legacy:
                conn.execute("""INSERT INTO analysis_cursors SELECT ?,source,instrument_uid,interval,assignment_id,open_time_utc
                    FROM analysis_cursors_v1""", (LEGACY_CONTOUR,))
                conn.execute("DROP TABLE analysis_cursors_v1")
            now = _utc_text()
            conn.execute("""INSERT OR IGNORE INTO contour_cursors(contour_id,source,instrument_uid,interval,processed_through,updated_at)
                SELECT ?,source,instrument_uid,interval,processed_through,? FROM market_cursors WHERE processed_through IS NOT NULL""",
                (LEGACY_CONTOUR, now))
            conn.execute("UPDATE market_cursors SET processed_through=NULL WHERE processed_through IS NOT NULL")
            rows = conn.execute("""SELECT key,value FROM metadata WHERE key LIKE 'live:candle:%'
                AND value LIKE '%"input_reference"%'""").fetchall()
            for key, value in rows:
                facts = json.loads(value)
                reference = facts.pop("input_reference", None)
                if reference is not None:
                    facts.setdefault("input_references", {})[LEGACY_CONTOUR] = reference
                    self._write_live(conn, key, facts)
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('contour_marks','1')")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _backup_before_migration(self, conn: sqlite3.Connection, table: str, *, label: str | None = None) -> None:
        """Согласованная копия файла базы перед пересозданием таблицы с данными."""
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label = label or f"before-schema-{SCHEMA_VERSION}"
        target = self.path.with_name(f"{self.path.name}.{label}-{stamp}.bak")
        with sqlite3.connect(target) as backup:
            conn.backup(backup)

    @staticmethod
    def _set_default(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)", (key, value))

    def source_info(self) -> SourceInfo:
        with self._lock, self._connect() as conn:
            # Только ключи источника: в metadata лежат и факты каждой свечи.
            values = dict(conn.execute("SELECT key, value FROM metadata WHERE key IN "
                                       "('producer_id', 'schema_version', 'min_after_id')").fetchall())
            maximum = conn.execute("SELECT COALESCE(MAX(change_id), 0) FROM changes").fetchone()[0]
        minimum = int(values["min_after_id"])
        # Очищенная целиком лента не опускает максимум ниже удержанного края.
        return SourceInfo(values["producer_id"], int(values["schema_version"]), minimum, max(maximum, minimum))

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

    def take_live_states(self, *, kind, source) -> list[tuple[str, str, dict]]:
        """Забирает и удаляет одной транзакцией все состояния вида: (uid, ТФ, значение)."""
        prefix = "live:" + kind + ":"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT key, value FROM metadata WHERE substr(key,1,?)=?",
                                    (len(prefix), prefix)).fetchall()
                taken = []
                for key, value in rows:
                    identity = json.loads(key[len(prefix):])
                    if identity[0] != source:
                        continue
                    taken.append((identity[1], identity[2], json.loads(value)))
                    conn.execute("DELETE FROM metadata WHERE key=?", (key,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return taken

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
            pins = facts.get("input_references") or {}
            consumer, pin = next(((contour, ref) for contour, ref in sorted(pins.items())
                                  if ref.get("state") == "consumed"), (None, None))
            if consumer is None:
                passed = conn.execute("""SELECT contour_id FROM contour_cursors WHERE source=? AND instrument_uid=?
                    AND interval=? AND processed_through>=? ORDER BY contour_id LIMIT 1""",
                    (source, uid, interval, stamp)).fetchone()
                consumer = None if passed is None else passed[0]
            started = next(((contour, ref) for contour, ref in sorted(pins.items())
                            if ref.get("state") == "started"), None)
            if interval == "1m" and consumer is not None:
                held = self._read_live(conn, uid_key)
                held.update(integrity_hold=True, incident={"open_time": stamp, "candidate_revision": revision,
                    "consumed_revision": None if pin is None else pin["revision"],
                    "reason": "consumed_input_changed" if pin else "legacy_input_unconfirmed",
                    "contour_id": consumer, "received_at": candle["received_at"]})
                self._write_live(conn, uid_key, held)
            elif interval == "1m" and started is not None:
                # Начатый пакет ещё не подтверждён финансовым owner.
                # Замораживаем зависимые новые действия, но не выдаём
                # сам факт pin за уже исполненную минуту.
                held = self._read_live(conn, uid_key)
                pending = held.setdefault("pending_input_conflicts", {})
                pending[stamp] = {"revision": started[1]["revision"], "candidate_revision": revision,
                                  "contour_id": started[0]}
                self._write_live(conn, uid_key, held)
            self._write_live(conn, facts_key, facts)
            return {"state": "conflict", "revision": revision, "changed": False}

        payload = {**candle, "revision": revision}
        # Прежнее состояние интервала — в той же транзакции: «свеча вне
        # расписания» видна без отдельного запроса на каждую свечу страницы.
        previous = conn.execute("""SELECT state FROM interval_coverage
            WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?""", identity).fetchone()
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
        return {"state": "accepted", "revision": revision, "changed": True,
                "previous_coverage": None if previous is None else previous["state"]}

    def pin_input(self, *, source, instrument_uid, interval, open_time, contour=None):
        """Закрепляет выбранную revision до финансовой обработки контура ``contour``."""
        _require_contour(contour)
        key = self._live_key("candle", source, instrument_uid, interval, open_time)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                facts = self._read_live(conn, key)
                reference = (facts.get("input_references") or {}).get(contour)
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
                    facts.setdefault("input_references", {})[contour] = reference
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
        симуляцию; статус записывается отдельно от временной свечи. Итог по
        данным источника они не понижают: тогда возвращается ``None``.
        """
        if state not in INTERVAL_STATES:
            raise ValueError(f"Неизвестное состояние интервала: {state}")
        when = _utc_text(observed_at)
        stamp = _utc_text(open_time)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._keeps_final_state(conn, source, instrument_uid, interval, stamp, state):
                    conn.commit()
                    return None
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

    def set_interval_states(self, *, source: str, instrument_uid: str, interval: str,
                            rows: Iterable[tuple[object, str, str]], observed_at: object | None = None) -> int:
        """Пакет состояний одного ряда в одной транзакции и с одним пересчётом префикса.

        Полученный интервал (``received``) не понижается, итог по данным
        источника (``confirmed_no_trade``, ``source_absent``) не возвращается в
        ``pending``/``unresolved``. Возвращает число изменённых строк. Нужен сверке длинных диапазонов: ночь и выходные —
        это тысячи минут, по транзакции на каждую слишком дорого.
        """
        prepared = []
        for open_time, state, evidence in rows:
            if state not in INTERVAL_STATES:
                raise ValueError(f"Неизвестное состояние интервала: {state}")
            prepared.append((_utc_text(open_time), state, evidence))
        if not prepared:
            return 0
        when = _utc_text(observed_at)
        changed = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stamp, state, evidence in prepared:
                    current = conn.execute(
                        "SELECT state FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                        (source, instrument_uid, interval, stamp)).fetchone()
                    if current is not None and current["state"] == "received":
                        continue
                    if current is not None and _downgrades_final(current["state"], state):
                        continue
                    if self._upsert_coverage(conn, source, instrument_uid, interval, stamp, state, evidence, when, emit=True):
                        changed += 1
                pair = self._read_live(conn, self._live_key("pair", source, instrument_uid, interval))
                cursor = conn.execute(
                    "SELECT received_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                    (source, instrument_uid, interval),
                ).fetchone()
                latest = max(stamp for stamp, _, _ in prepared)
                horizon = None if cursor is None or cursor[0] is None else max(latest, cursor[0])
                if pair.get("coverage_lower") and horizon is not None and horizon >= _utc_text(pair["coverage_lower"]):
                    self._resolve_contiguous_prefix(conn, source, instrument_uid, interval,
                                                    pair["coverage_lower"], horizon)
                conn.commit()
                return changed
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _keeps_final_state(conn: sqlite3.Connection, source: str, instrument_uid: str, interval: str,
                           stamp: str, state: str) -> bool:
        if state not in WAITING_INTERVAL_STATES:
            return False
        current = conn.execute(
            "SELECT state FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
            (source, instrument_uid, interval, stamp)).fetchone()
        return current is not None and _downgrades_final(current["state"], state)

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
        status = {"source": source, "instrument_uid": instrument_uid, "interval": interval, "open_time_utc": stamp,
                  "state": EXPORTED_STATE.get(state, state), "evidence": evidence, "observed_at": observed_at}
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

    def record_schedule_day(
        self, *, source: str, exchange: str, day: object, observed_at: object | None = None,
        provenance: str = "broker", rules_version: str | None = None,
    ) -> bool:
        """Сохраняет новую версию сырого дня либо подтверждает прежнюю.

        Возвращает ``True`` только если содержание дня изменилось и получило
        новый отдельный курсор ленты расписаний. Восстановленный день сюда не
        попадает: он не наблюдён у брокера и не должен уйти в ленту экспорта.
        """
        _require_broker(provenance)
        exchange = normalize_exchange(exchange)
        raw_json = canonical_day_json(day)
        version = day_version(day)
        day_key = trading_date(day).isoformat()
        is_trading_day = int(bool(getattr(day, "is_trading_day", False)))
        when = _utc_text(observed_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """SELECT 1 FROM trading_schedule_days
                    WHERE source=? AND exchange=? AND trading_date=? AND version=?""",
                    (source, exchange, day_key, version),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        """UPDATE trading_schedule_days SET last_confirmed_at=?
                        WHERE source=? AND exchange=? AND trading_date=? AND version=?""",
                        (when, source, exchange, day_key, version),
                    )
                    conn.commit()
                    return False
                change_id = self._next_schedule_change_id(conn)
                conn.execute(
                    """INSERT INTO trading_schedule_days(
                    source,exchange,trading_date,version,raw_json,is_trading_day,
                    observed_at,last_confirmed_at,schedule_change_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (source, exchange, day_key, version, raw_json, is_trading_day, when, when, change_id),
                )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def schedule_days(self, *, source: str, exchange: str) -> tuple[StoredScheduleDay, ...]:
        """Последняя версия каждого сохранённого дня режима торгов."""
        exchange = normalize_exchange(exchange)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT source,exchange,trading_date,version,raw_json,is_trading_day,
                observed_at,last_confirmed_at,schedule_change_id FROM trading_schedule_days
                WHERE source=? AND exchange=? AND schedule_change_id IN (
                    SELECT MAX(schedule_change_id) FROM trading_schedule_days
                    WHERE source=? AND exchange=? GROUP BY trading_date
                ) ORDER BY trading_date""",
                (source, exchange, source, exchange),
            ).fetchall()
        return tuple(
            StoredScheduleDay(
                source=row["source"], exchange=row["exchange"], trading_date=row["trading_date"],
                version=row["version"], raw_json=row["raw_json"], is_trading_day=bool(row["is_trading_day"]),
                observed_at=row["observed_at"], last_confirmed_at=row["last_confirmed_at"],
                schedule_change_id=row["schedule_change_id"],
            )
            for row in rows
        )

    def record_schedule_binding(
        self, *, source: str, instrument_uid: str, exchange: str, observed_at: object | None = None,
        provenance: str = "broker",
    ) -> bool:
        """Фиксирует смену режима торгов UID без дублирования прежней связи."""
        _require_broker(provenance)
        exchange = normalize_exchange(exchange)
        when = _utc_text(observed_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                latest = conn.execute(
                    """SELECT exchange FROM instrument_schedule_bindings
                    WHERE source=? AND instrument_uid=? ORDER BY observed_at DESC LIMIT 1""",
                    (source, instrument_uid),
                ).fetchone()
                if latest is not None and latest["exchange"] == exchange:
                    conn.commit()
                    return False
                change_id = self._next_schedule_change_id(conn)
                conn.execute(
                    """INSERT INTO instrument_schedule_bindings(
                    source,instrument_uid,exchange,observed_at,schedule_change_id)
                    VALUES (?,?,?,?,?)""",
                    (source, instrument_uid, exchange, when, change_id),
                )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def schedule_binding(self, *, source: str, instrument_uid: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT exchange FROM instrument_schedule_bindings
                WHERE source=? AND instrument_uid=? ORDER BY observed_at DESC LIMIT 1""",
                (source, instrument_uid),
            ).fetchone()
        return None if row is None else normalize_exchange(row["exchange"])

    def schedule_last_confirmed_at(self, *, source: str, exchange: str) -> str | None:
        exchange = normalize_exchange(exchange)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT MAX(last_confirmed_at) AS last_confirmed_at FROM trading_schedule_days
                WHERE source=? AND exchange=?""",
                (source, exchange),
            ).fetchone()
        return None if row is None or row["last_confirmed_at"] is None else str(row["last_confirmed_at"])

    @staticmethod
    def _next_schedule_change_id(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM metadata WHERE key='schedule_max_change_id'").fetchone()
        current = int(row[0]) if row is not None else 0
        next_id = current + 1
        conn.execute(
            """INSERT INTO metadata(key,value) VALUES ('schedule_max_change_id',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(next_id),),
        )
        return next_id

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

    def analysis_cursor(self, *, source, instrument_uid, interval, assignment_id, contour=None):
        _require_contour(contour)
        with self._lock, self._connect() as conn:
            row = conn.execute("""SELECT open_time_utc FROM analysis_cursors WHERE contour_id=? AND source=?
                AND instrument_uid=? AND interval=? AND assignment_id=?""",
                (contour, source, instrument_uid, interval, assignment_id)).fetchone()
        return pd.Timestamp(row[0]).tz_localize(None) if row else None

    def mark_analyzed(self, *, source, instrument_uid, interval, assignment_id, open_time, contour=None):
        _require_contour(contour)
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO analysis_cursors VALUES(?,?,?,?,?,?)
                ON CONFLICT(contour_id,source,instrument_uid,interval,assignment_id) DO UPDATE SET
                open_time_utc=MAX(analysis_cursors.open_time_utc,excluded.open_time_utc)""",
                (contour, source, instrument_uid, interval, assignment_id, _utc_text(open_time)))

    def unresolved_between(self, *, source, instrument_uid, interval, start, end, limit=1000):
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT open_time_utc FROM interval_coverage WHERE
                source=? AND instrument_uid=? AND interval=? AND state IN ('pending','unresolved')
                AND open_time_utc>=? AND open_time_utc<? ORDER BY open_time_utc LIMIT ?""",
                (source, instrument_uid, interval, _utc_text(start), _utc_text(end), limit)).fetchall()
        return [pd.Timestamp(row[0]).tz_localize(None) for row in rows]

    def mark_processed(self, *, source: str, instrument_uid: str, interval: str, open_time: object,
                       contour: str | None = None) -> None:
        """Продвигает границу обработки контура после его durable commit."""
        _require_contour(contour)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stamp = _utc_text(open_time)
                key = self._live_key("candle", source, instrument_uid, interval, stamp)
                facts = self._read_live(conn, key)
                reference = (facts.get("input_references") or {}).get(contour)
                if reference is not None:
                    reference["state"] = "consumed"
                    self._write_live(conn, key, facts)
                uid_key = self._live_key("uid", source, instrument_uid)
                held = self._read_live(conn, uid_key)
                pending = held.get("pending_input_conflicts", {})
                conflict = pending.get(stamp)
                if conflict is not None and conflict.get("contour_id", contour) == contour:
                    pending.pop(stamp)
                    held.update(integrity_hold=True, incident={"open_time": stamp,
                        "consumed_revision": conflict["revision"], "candidate_revision": conflict["candidate_revision"],
                        "reason": "consumed_input_changed", "contour_id": contour, "received_at": _utc_text()})
                    self._write_live(conn, uid_key, held)
                conn.execute("""INSERT INTO contour_cursors(contour_id,source,instrument_uid,interval,processed_through,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(contour_id,source,instrument_uid,interval) DO UPDATE SET
                    processed_through=MAX(contour_cursors.processed_through,excluded.processed_through),
                    updated_at=excluded.updated_at""", (contour, source, instrument_uid, interval, stamp, _utc_text()))
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

    def publish_pair_readiness(self, *, source: str, instrument_uid: str, interval: str, status: dict) -> None:
        """Текущая готовность живой пары от ядра; не событие и не строка ``/changes``."""
        payload = json.dumps(status, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO pair_readiness(source,instrument_uid,interval,status_json,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(source,instrument_uid,interval) DO UPDATE SET
                status_json=excluded.status_json, updated_at=excluded.updated_at
                WHERE pair_readiness.status_json<>excluded.status_json""",
                (source, instrument_uid, interval, payload, _utc_text()))

    # ── Потребности торговых контуров (add-contour-process) ──
    # Пишет только контур своего contour_id, читает ядро: так процесс контура
    # передаёт спрос пар и обязательства сделок без вызова в память ядра.

    def record_contour_demand(self, contour_id: str, demands) -> None:
        """Спрос пар ``(ключ инструмента, ТФ, глубина, карточка инструмента)``; глубина не уменьшается."""
        rows = [(contour_id, key, interval, int(min_bars), json.dumps(instrument, ensure_ascii=False, sort_keys=True),
                 _utc_text()) for key, interval, min_bars, instrument in demands]
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO contour_demand(contour_id,instrument_key,interval,min_bars,instrument_json,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(contour_id,instrument_key,interval) DO UPDATE SET
                min_bars=MAX(contour_demand.min_bars,excluded.min_bars),instrument_json=excluded.instrument_json,
                updated_at=excluded.updated_at
                WHERE excluded.min_bars>contour_demand.min_bars OR excluded.instrument_json<>contour_demand.instrument_json""",
                rows)

    def contour_demands(self) -> list[dict]:
        """Спрос всех контуров: ``contour_id``, ``instrument_key``, ``interval``, ``min_bars``, ``instrument``."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT contour_id,instrument_key,interval,min_bars,instrument_json FROM contour_demand
                ORDER BY contour_id,instrument_key,interval""").fetchall()
        return [{"contour_id": row[0], "instrument_key": row[1], "interval": row[2], "min_bars": int(row[3]),
                 "instrument": json.loads(row[4])} for row in rows]

    def replace_contour_obligations(self, contour_id: str, obligations) -> None:
        """Снимок обязательств контура ``{ключ: (граница, включительно)}`` одной транзакцией."""
        now = _utc_text()
        rows = [(contour_id, key, _utc_text(stamp), 1 if inclusive else 0, now)
                for key, (stamp, inclusive) in sorted(obligations.items()) if stamp is not None]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM contour_obligations WHERE contour_id=?", (contour_id,))
                conn.executemany("""INSERT INTO contour_obligations(contour_id,instrument_key,lower_open_time,inclusive,updated_at)
                    VALUES (?,?,?,?,?)""", rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def contour_obligations(self) -> dict[str, dict[str, tuple[datetime, bool]]]:
        """Обязательства по контурам: ``{contour_id: {ключ: (граница naive UTC, включительно)}}``."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT contour_id,instrument_key,lower_open_time,inclusive FROM contour_obligations
                ORDER BY contour_id,instrument_key""").fetchall()
        result: dict[str, dict[str, tuple[datetime, bool]]] = {}
        for contour_id, key, lower, inclusive in rows:
            stamp = pd.Timestamp(lower).tz_convert("UTC").tz_localize(None).to_pydatetime()
            result.setdefault(contour_id, {})[key] = (stamp, bool(inclusive))
        return result

    def consumer_checkpoint(self, consumer_id: str) -> dict | None:
        """Подтверждённый курсор потребителя ленты: ``acknowledged_through`` и ``acknowledged_at``."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT acknowledged_through, acknowledged_at FROM consumer_checkpoints WHERE consumer_id=?",
                               (consumer_id,)).fetchone()
        return None if row is None else {"acknowledged_through": int(row[0]), "acknowledged_at": row[1]}

    def drop_pair_readiness(self, *, source: str, instrument_uid: str | None = None,
                            interval: str | None = None) -> None:
        """Снимает готовность пары; без UID — всех пар источника (старт ядра)."""
        with self._lock, self._connect() as conn:
            if instrument_uid is None:
                conn.execute("DELETE FROM pair_readiness WHERE source=?", (source,))
            else:
                conn.execute("DELETE FROM pair_readiness WHERE source=? AND instrument_uid=? AND interval=?",
                             (source, instrument_uid, interval))

    def pair_readiness(self, *, source: str, instrument_uid: str, interval: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("""SELECT status_json,updated_at FROM pair_readiness
                WHERE source=? AND instrument_uid=? AND interval=?""", (source, instrument_uid, interval)).fetchone()
        return None if row is None else {**json.loads(row[0]), "updated_at": row[1]}

    def pair_readiness_all(self, *, source: str) -> list[dict]:
        """Готовность всех живых пар источника одной короткой транзакцией чтения."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT instrument_uid,interval,status_json,updated_at FROM pair_readiness
                WHERE source=? ORDER BY instrument_uid,interval""", (source,)).fetchall()
        return [{**json.loads(row["status_json"]), "instrument_uid": row["instrument_uid"],
                 "interval": row["interval"], "updated_at": row["updated_at"]} for row in rows]

    def checkpoint(self, consumer_id: str) -> dict | None:
        """Подтверждённый курсор ленты ``/changes`` потребителя либо ``None``."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT producer_id,acknowledged_through FROM consumer_checkpoints WHERE consumer_id=?",
                               (consumer_id,)).fetchone()
        return None if row is None else {"producer_id": row[0], "acknowledged_through": int(row[1])}

    def source_absent_opens(self, *, source: str, instrument_uid: str, interval: str) -> list[pd.Timestamp]:
        """Интервалы, разрешённые как «у источника нет»: поздняя свеча по ним — отдельная отметка."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT open_time_utc FROM interval_coverage WHERE source=? AND instrument_uid=?
                AND interval=? AND state='source_absent' ORDER BY open_time_utc""",
                (source, instrument_uid, interval)).fetchall()
        return [pd.Timestamp(row[0]).tz_convert("UTC").tz_localize(None) for row in rows]

    def cursors(self, *, source: str, instrument_uid: str, interval: str, contour: str | None = None) -> dict[str, str | None]:
        """Границы пары; ``processed_through`` — контура ``contour`` либо наибольшая по контурам."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT received_through,resolved_through FROM market_cursors WHERE source=? AND instrument_uid=? AND interval=?",
                (source, instrument_uid, interval),
            ).fetchone()
            processed = conn.execute(
                "SELECT MAX(processed_through) FROM contour_cursors WHERE source=? AND instrument_uid=? AND interval=?"
                + ("" if contour is None else " AND contour_id=?"),
                (source, instrument_uid, interval) + (() if contour is None else (contour,)),
            ).fetchone()[0]
        result = dict(row) if row is not None else {"received_through": None, "resolved_through": None}
        return {**result, "processed_through": processed}

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

    def schedule_changes_after(self, producer_id: str, after_id: int, limit: int = 500) -> dict[str, Any]:
        """Отдельная лента расписаний: свечной курсор её не затрагивает."""
        info = self.source_info()
        if producer_id != info.producer_id:
            raise ProducerMismatch()
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as conn:
            rows = conn.execute("""SELECT schedule_change_id AS change_id, 'schedule_day' AS kind,
                source,exchange,NULL AS instrument_uid,trading_date,version,raw_json,is_trading_day,observed_at,last_confirmed_at
                FROM trading_schedule_days WHERE schedule_change_id>?
                UNION ALL SELECT schedule_change_id, 'schedule_binding', source,exchange,instrument_uid,NULL,NULL,NULL,NULL,observed_at,NULL
                FROM instrument_schedule_bindings WHERE schedule_change_id>?
                ORDER BY change_id LIMIT ?""", (after_id, after_id, limit + 1)).fetchall()
        has_more, rows = len(rows) > limit, rows[:limit]
        changes = []
        for row in rows:
            value = {"change_id": row["change_id"], "kind": row["kind"], "source": row["source"],
                     "exchange": row["exchange"], "observed_at": row["observed_at"]}
            if row["kind"] == "schedule_binding":
                value["instrument_uid"] = row["instrument_uid"]
            if row["kind"] == "schedule_day":
                value["day"] = {"trading_date": row["trading_date"], "version": row["version"],
                                "raw_json": row["raw_json"], "is_trading_day": bool(row["is_trading_day"]),
                                "last_confirmed_at": row["last_confirmed_at"]}
            changes.append(value)
        return {"producer_id": info.producer_id, "next_after_id": changes[-1]["change_id"] if changes else after_id,
                "has_more": has_more, "changes": changes}

    def acknowledge_schedule(self, consumer_id: str, producer_id: str, change_id: int) -> dict[str, Any]:
        """Подтверждает только ленту расписаний в отдельном namespace курсора."""
        if not consumer_id or not consumer_id.replace("-", "").isalnum() or consumer_id.lower() != consumer_id:
            raise ValueError("consumer_id должен содержать строчные латинские буквы, цифры и дефисы")
        info = self.source_info()
        if producer_id != info.producer_id:
            raise ProducerMismatch()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='schedule_max_change_id'").fetchone()
            maximum = int(row[0]) if row is not None else 0
            if change_id < 0 or change_id > maximum:
                raise ValueError("change_id вне доступного диапазона расписаний")
            key, now = f"{SCHEDULE_CHECKPOINT_PREFIX}{consumer_id}", _utc_text()
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute("SELECT acknowledged_through FROM consumer_checkpoints WHERE consumer_id=?", (key,)).fetchone()
                acknowledged = max(change_id, int(old[0])) if old else change_id
                conn.execute("""INSERT INTO consumer_checkpoints(consumer_id,producer_id,acknowledged_through,acknowledged_at)
                    VALUES(?,?,?,?) ON CONFLICT(consumer_id) DO UPDATE SET
                    producer_id=excluded.producer_id,acknowledged_through=MAX(consumer_checkpoints.acknowledged_through,excluded.acknowledged_through),acknowledged_at=excluded.acknowledged_at""",
                    (key, producer_id, acknowledged, now))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"producer_id": info.producer_id, "consumer_id": consumer_id, "acknowledged_through": acknowledged}

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

    def prune_changes(self, older_than: object, *, limit: int = PRUNE_BATCH) -> ChangesPrune:
        """Удаляет порцию начала ленты ``/changes`` старше ``older_than``.

        Удаляется только префикс ленты и не дальше наименьшего подтверждения
        потребителей ``/changes`` текущего производителя: неподтверждённое не
        удаляется. Подтверждения ленты расписаний очистку не удерживают.
        ``min_after_id`` поднимается в той же транзакции (add-changes-retention).
        """
        cutoff = _utc_text(older_than)
        limit = max(1, int(limit))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                values = dict(conn.execute(
                    "SELECT key, value FROM metadata WHERE key IN ('producer_id', 'min_after_id')").fetchall())
                floor = int(values["min_after_id"])
                held = conn.execute(
                    "SELECT consumer_id, acknowledged_through FROM consumer_checkpoints "
                    "WHERE producer_id=? AND consumer_id NOT LIKE ? "
                    "ORDER BY acknowledged_through, consumer_id LIMIT 1",
                    (values["producer_id"], SCHEDULE_CHECKPOINT_PREFIX + "%"),
                ).fetchone()
                bound = None if held is None else int(held["acknowledged_through"])
                rows = conn.execute(
                    "SELECT change_id, recorded_at FROM changes ORDER BY change_id LIMIT ?", (limit,)).fetchall()
                last, blocked = None, False
                for row in rows:
                    if row["recorded_at"] >= cutoff:
                        break
                    if bound is not None and row["change_id"] > bound:
                        blocked = True
                        break
                    last = int(row["change_id"])
                deleted = 0
                if last is not None:
                    deleted = conn.execute("DELETE FROM changes WHERE change_id<=?", (last,)).rowcount
                    floor = max(floor, last)
                    conn.execute("UPDATE metadata SET value=? WHERE key='min_after_id'", (str(floor),))
                lag = 0
                if blocked:
                    newest = conn.execute("SELECT COALESCE(MAX(change_id), 0) FROM changes").fetchone()[0]
                    lag = max(int(newest) - bound, 0)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        more = last is not None and len(rows) == limit and last == int(rows[-1]["change_id"])
        return ChangesPrune(deleted, floor, more, held["consumer_id"] if blocked else None, lag)

    # ── состав пар и проверки полноты (add-independent-market-data-lanes) ──
    def save_roster(self, *, source: str, entries: Iterable[dict], observed_at: object | None = None) -> None:
        """Сохраняет пары состава; остальные пары, кроме обязательств, становятся прошлыми.

        ``entries``: ``instrument_uid``, ``interval``, ``ticker``,
        ``instrument_type``, ``origin`` (``selected``/``obligation``),
        необязательные ``short_name``, ``window_lower``, ``requirements``.
        """
        when = _utc_text(observed_at)
        rows = []
        for entry in entries:
            origin = entry.get("origin", "selected")
            if origin not in ROSTER_ORIGINS:
                raise ValueError(f"Неизвестное происхождение пары состава: {origin}")
            lower = entry.get("window_lower")
            rows.append((source, entry["instrument_uid"], entry["interval"], entry["ticker"],
                         entry["instrument_type"], entry.get("short_name"), origin,
                         None if lower is None else _utc_text(lower),
                         json.dumps(entry.get("requirements") or {}, ensure_ascii=False, sort_keys=True), when))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                keep = {(row[1], row[2]) for row in rows}
                for uid, interval in conn.execute(
                        "SELECT instrument_uid, interval FROM market_data_roster WHERE source=? AND origin='selected'",
                        (source,)).fetchall():
                    if (uid, interval) not in keep:
                        conn.execute("""UPDATE market_data_roster SET origin='previous'
                            WHERE source=? AND instrument_uid=? AND interval=?""", (source, uid, interval))
                conn.executemany(
                    """INSERT INTO market_data_roster(source,instrument_uid,interval,ticker,instrument_type,short_name,
                    origin,window_lower_utc,requirements_json,confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source,instrument_uid,interval) DO UPDATE SET ticker=excluded.ticker,
                    instrument_type=excluded.instrument_type,short_name=excluded.short_name,origin=excluded.origin,
                    window_lower_utc=COALESCE(excluded.window_lower_utc, market_data_roster.window_lower_utc),
                    requirements_json=excluded.requirements_json,confirmed_at=excluded.confirmed_at""", rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def mark_roster_previous(self, *, source: str, pairs: Iterable[tuple[str, str]]) -> None:
        """Пары, снятые запуском как ненужные, становятся прошлыми, даже обязательства."""
        with self._lock, self._connect() as conn:
            conn.executemany("""UPDATE market_data_roster SET origin='previous'
                WHERE source=? AND instrument_uid=? AND interval=?""",
                [(source, uid, interval) for uid, interval in pairs])

    def set_roster_window(self, *, source: str, instrument_uid: str, interval: str, window_lower: object) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""UPDATE market_data_roster SET window_lower_utc=?
                WHERE source=? AND instrument_uid=? AND interval=?""",
                (_utc_text(window_lower), source, instrument_uid, interval))

    def roster(self, *, source: str, origins: Iterable[str] = ("selected", "obligation")) -> list[dict]:
        wanted = tuple(origins)
        marks = ",".join("?" for _ in wanted)
        with self._lock, self._connect() as conn:
            rows = conn.execute(f"""SELECT * FROM market_data_roster WHERE source=? AND origin IN ({marks})
                ORDER BY instrument_uid, interval""", (source, *wanted)).fetchall()
        return [{"instrument_uid": row["instrument_uid"], "interval": row["interval"], "ticker": row["ticker"],
                 "instrument_type": row["instrument_type"], "short_name": row["short_name"], "origin": row["origin"],
                 "window_lower": None if row["window_lower_utc"] is None
                 else pd.Timestamp(row["window_lower_utc"]).tz_convert("UTC").tz_localize(None),
                 "requirements": json.loads(row["requirements_json"]), "confirmed_at": row["confirmed_at"]}
                for row in rows]

    def window_snapshot(self, *, source: str, instrument_uid: str, interval: str, lower: object, upper: object) -> dict:
        """Полученные свечи, состояния покрытия и проверки окна пары без сети.

        Границы включительные, моменты — UTC-naive открытия баров.
        """
        low, high = _utc_text(lower), _utc_text(upper)
        identity = (source, instrument_uid, interval, low, high)
        with self._lock, self._connect() as conn:
            received = conn.execute("""SELECT open_time_utc FROM candle_current WHERE source=? AND instrument_uid=?
                AND interval=? AND is_complete=1 AND open_time_utc>=? AND open_time_utc<=?""", identity).fetchall()
            states = conn.execute("""SELECT open_time_utc, state, evidence FROM interval_coverage WHERE source=?
                AND instrument_uid=? AND interval=? AND open_time_utc>=? AND open_time_utc<=?""", identity).fetchall()
            checks = conn.execute("""SELECT * FROM interval_checks WHERE source=? AND instrument_uid=? AND interval=?
                AND open_time_utc>=? AND open_time_utc<=?""", identity).fetchall()

        def stamp(value):
            return pd.Timestamp(value).tz_convert("UTC").tz_localize(None)

        return {"received": {stamp(row[0]) for row in received},
                "states": {stamp(row["open_time_utc"]): (row["state"], row["evidence"]) for row in states},
                "checks": {stamp(row["open_time_utc"]): {
                    "checks": row["checks"], "first_checked_at": stamp(row["first_checked_at"]),
                    "last_checked_at": stamp(row["last_checked_at"]),
                    "next_check_at": None if row["next_check_at"] is None else stamp(row["next_check_at"])}
                    for row in checks}}

    def record_interval_checks(self, *, source: str, instrument_uid: str, interval: str,
                               open_times: Iterable[object], checked_at: object, next_check_at: object | None) -> None:
        """Пакетная отметка пустой проверки интервалов: +1 к числу, новый срок."""
        when = _utc_text(checked_at)
        due = None if next_check_at is None else _utc_text(next_check_at)
        rows = [(source, instrument_uid, interval, _utc_text(stamp), when, when, due) for stamp in open_times]
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("""INSERT INTO interval_checks(source,instrument_uid,interval,open_time_utc,checks,
                    first_checked_at,last_checked_at,next_check_at) VALUES (?,?,?,?,1,?,?,?)
                    ON CONFLICT(source,instrument_uid,interval,open_time_utc) DO UPDATE SET
                    checks=interval_checks.checks+1,last_checked_at=excluded.last_checked_at,
                    next_check_at=excluded.next_check_at""", rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

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

    def pattern_context(self, *, source: str, instrument_uid: str, interval: str,
                        close_time: object) -> dict[str, object]:
        """Две завершённые свечи и их покрытие из одного SQLite-снимка."""
        upper = _utc_text(close_time)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            try:
                rows = conn.execute(
                    """SELECT open_time_utc, open, high, low, close
                    FROM candle_current WHERE source=? AND instrument_uid=? AND interval=?
                    AND is_complete=1 AND open_time_utc < ?
                    ORDER BY open_time_utc DESC LIMIT 2""",
                    (source, instrument_uid, interval, upper),
                ).fetchall()
                rows = list(reversed(rows))
                coverage = {}
                if rows:
                    marks = [row["open_time_utc"] for row in rows]
                    placeholders = ",".join("?" for _ in marks)
                    coverage_rows = conn.execute(
                        f"""SELECT open_time_utc, state FROM interval_coverage
                        WHERE source=? AND instrument_uid=? AND interval=?
                        AND open_time_utc IN ({placeholders})""",
                        (source, instrument_uid, interval, *marks),
                    ).fetchall()
                    coverage = {row["open_time_utc"]: row["state"] for row in coverage_rows}
                    unresolved = conn.execute(
                        """SELECT open_time_utc FROM interval_coverage
                        WHERE source=? AND instrument_uid=? AND interval=?
                        AND open_time_utc>=? AND open_time_utc<?
                        AND state IN ('pending','unresolved')
                        ORDER BY open_time_utc LIMIT 1""",
                        (source, instrument_uid, interval, rows[0]["open_time_utc"], upper),
                    ).fetchone()
                else:
                    unresolved = None
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        candles = [
            {"datetime": row["open_time_utc"], "open": float(row["open"]), "high": float(row["high"]),
             "low": float(row["low"]), "close": float(row["close"])}
            for row in rows
        ]
        missing = next((row["open_time_utc"] for row in rows
                        if coverage.get(row["open_time_utc"]) in {None, "pending", "unresolved"}), None)
        if missing is None and unresolved is not None:
            missing = unresolved["open_time_utc"]
        if missing is None and len(rows) != 2:
            missing = upper
        return {"candles": candles, "has_gap": missing is not None, "missing_open_time": missing}

    def enqueue_pattern_event(self, *, event_id: str, payload_json: str) -> None:
        now = _utc_text()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO pattern_event_outbox(event_id,payload_json,attempts,next_attempt_at)
                VALUES (?,?,0,?) ON CONFLICT(event_id) DO NOTHING""",
                (event_id, payload_json, now),
            )

    def pending_pattern_events(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT event_id,payload_json,attempts FROM pattern_event_outbox
                WHERE delivered_at IS NULL AND next_attempt_at<=?
                ORDER BY next_attempt_at,event_id LIMIT ?""", (_utc_text(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_pattern_event_delivered(self, event_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE pattern_event_outbox SET delivered_at=?, last_error=NULL WHERE event_id=?",
                         (_utc_text(), event_id))

    def defer_pattern_event(self, event_id: str, error: str) -> None:
        retry_at = _utc_text(datetime.now(timezone.utc) + pd.Timedelta(seconds=1))
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE pattern_event_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=?
                WHERE event_id=?""", (retry_at, error[:1000], event_id),
            )


class ProducerMismatch(Exception):
    """Курсор принадлежит другой базе источника."""


class CursorExpired(Exception):
    """Потребитель запросил удалённую часть журнала."""
