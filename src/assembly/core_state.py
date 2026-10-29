"""Желаемое состояние контуров и журнал команд ядра — ``data/core.sqlite3`` (add-control-api).

Пишет только ядро; процесс контура открывает базу только на чтение
(``read_desired``). Каждая команда, меняющая желаемое, увеличивает номер
поколения: контур применяет его на границе тика и сообщает применённое
поколение в своём ``status.json``.

Таблица ``heartbeat`` — пульс ядра (add-contour-recovery): pid, версия, время
старта и последнего удара, ``running`` / ``stopped``. Контур читает её
``read_heartbeat`` и без свежего удара перестаёт входить в новые сделки.

Таблица ``history_runs`` — журнал прогонов истории (add-history-contour):
запрос, состояние, времена, pid, итог и флаг стопа. Процесс прогона читает
свою строку только на чтение (``read_history_stop``).
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
# Состояния пульса ядра.
BEAT_RUNNING, BEAT_STOPPED = "running", "stopped"
# Состояния прогона истории; первые четыре — прогон ещё не завершён.
RUN_QUEUED, RUN_STARTING, RUN_RUNNING, RUN_STOPPING = "queued", "starting", "running", "stopping"
RUN_FINISHED, RUN_STOPPED, RUN_FAILED = "finished", "stopped", "failed"
RUN_CANCELLED, RUN_INTERRUPTED = "cancelled", "interrupted"
RUN_ACTIVE = (RUN_QUEUED, RUN_STARTING, RUN_RUNNING, RUN_STOPPING)


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
    members: tuple = ()

    def payload(self) -> dict:
        return {"running": self.running, "stop_mode": self.stop_mode, "generation": self.generation}

    @property
    def active(self) -> tuple:
        return tuple(member for member in self.members if member.active)


@dataclass(frozen=True)
class Heartbeat:
    """Последний удар пульса ядра."""

    core_pid: int
    core_version: str
    started_at: str
    beat_at: str
    state: str

    @property
    def stopped(self) -> bool:
        return self.state == BEAT_STOPPED

    def age(self, now: datetime | None = None) -> float:
        """Секунды с последнего удара по часам машины."""
        moment = now or datetime.now(timezone.utc)
        return (moment - parse_time(self.beat_at)).total_seconds()

    def payload(self) -> dict:
        return {"core_pid": self.core_pid, "core_version": self.core_version, "started_at": self.started_at,
                "beat_at": self.beat_at, "state": self.state}


def parse_time(text: str) -> datetime:
    """Время базы ядра и ``status.json`` (ISO с ``Z``) → aware UTC."""
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _heartbeat(row) -> Heartbeat | None:
    if row is None:
        return None
    return Heartbeat(int(row["core_pid"]), row["core_version"], row["started_at"], row["beat_at"], row["state"])


def read_heartbeat(path: Path) -> Heartbeat | None:
    """Сторона контура: пульс ядра только на чтение; нет базы, таблицы или строки — ``None``."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return _heartbeat(conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone())
        except sqlite3.OperationalError:
            return None  # база ядра версии до add-contour-recovery


@dataclass(frozen=True)
class HistoryRun:
    """Строка журнала прогонов истории."""

    run_id: str
    directory: str
    request: dict
    state: str
    accepted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    reason: str | None = None
    stop_requested: bool = False
    market_data_sync: dict | None = None

    @property
    def active(self) -> bool:
        return self.state in RUN_ACTIVE


def _history_run(row) -> HistoryRun | None:
    if row is None:
        return None
    return HistoryRun(row["run_id"], row["directory"], json.loads(row["request_json"]), row["state"],
                      row["accepted_at"], row["started_at"], row["finished_at"], row["pid"], row["exit_code"],
                      row["reason"], bool(row["stop_requested"]),
                      None if row["sync_json"] is None else json.loads(row["sync_json"]))


def read_history_stop(path: Path, run_id: str) -> bool:
    """Сторона прогона: запрошен ли стоп своей строки; база только на чтение."""
    path = Path(path)
    if not path.exists():
        return False
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        try:
            row = conn.execute("SELECT stop_requested FROM history_runs WHERE run_id=?", (run_id,)).fetchone()
        except sqlite3.OperationalError:
            return False
    return bool(row and row[0])


@dataclass(frozen=True)
class Member:
    """Участник желаемого состава контура (add-contour-membership).

    ``instrument`` — карточка ``instrument_payload``; ``chosen`` — как
    инструмент назван в ответах API (токен, имя, тип, дни до экспирации);
    ``contract`` — карточки контракта ``contracts_payload`` от ядра.
    """

    key: str
    instrument: dict
    chosen: dict | None = None
    contract: dict | None = None
    active: bool = True
    remove_mode: str | None = None
    changed_generation: int = 0

    @property
    def name(self) -> str:
        if self.chosen and self.chosen.get("name"):
            return self.chosen["name"]
        return self.instrument.get("short_name") or self.instrument.get("ticker") or self.key


@dataclass(frozen=True)
class MembershipOutcome:
    """Итог команды состава: ``accepted`` — новое поколение, ``ignored`` — без изменений, ``conflict`` — отказ."""

    outcome: str
    generation: int
    reason: str = ""


def _member_key(payload: dict) -> str:
    """Ключ участника — как ``instrument_key``: UID, иначе тикер (у старых карточек — имя или токен)."""
    for name in ("uid", "ticker", "name", "token"):
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    raise ValueError("Карточка инструмента без UID, тикера и имени")


def match_members(members, names) -> tuple[list[str], list[tuple[str, str]]]:
    """Имена снятия → ключи участников; имя сравнивается только с ``name`` участника (rework-contour-api).

    Возвращает ключи (без повторов, в порядке имён) и отказы ``(имя, причина)``.
    """
    by_name = {member.name.upper(): member for member in members}
    keys, rejections = [], []
    for name in names:
        member = by_name.get(name.strip().upper())
        if member is None:
            rejections.append((name, "Инструмента нет в составе эмуляции."))
        elif member.key not in keys:
            keys.append(member.key)
    return keys, rejections


class RevisionChanged(Exception):
    """``If-Match`` не совпал с текущим поколением: команда не записана, в журнале — ``conflict``."""

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(f"Ожидалась ревизия {expected}, текущая {current}.")
        self.expected = expected
        self.current = current


@dataclass(frozen=True)
class StopOutcome:
    """Итог команды остановки: ``accepted`` — новое поколение, ``ignored`` — повтор, ``conflict`` — отказ."""

    outcome: str
    generation: int
    reason: str = ""


def _member(row) -> Member:
    return Member(row["instrument_key"], json.loads(row["instrument_json"]), json.loads(row["chosen_json"] or "null"),
                  json.loads(row["contract_json"] or "null"), row["desired"] == "active", row["remove_mode"],
                  int(row["changed_generation"]))


def _members(conn, contour_id) -> tuple:
    try:
        rows = conn.execute("SELECT * FROM contour_members WHERE contour_id=? ORDER BY rowid", (contour_id,)).fetchall()
    except sqlite3.OperationalError:
        return ()  # база ядра версии до add-contour-membership
    return tuple(_member(row) for row in rows)


def _desired(conn, row) -> Desired | None:
    if row is None:
        return None
    return Desired(row["contour_id"], row["kind"], row["desired"] == "running", row["stop_mode"],
                   int(row["generation"]), tuple(json.loads(row["instruments_json"])), row["source"],
                   row["command"], row["updated_at"], _members(conn, row["contour_id"]))


def read_desired(path: Path, contour_id: str) -> Desired | None:
    """Сторона контура: желаемое из базы ядра только на чтение; нет базы или строки — ``None``."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        return _desired(conn, conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())


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
                CREATE TABLE IF NOT EXISTS contour_members (
                    contour_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    instrument_json TEXT NOT NULL,
                    chosen_json TEXT,
                    contract_json TEXT,
                    desired TEXT NOT NULL CHECK(desired IN ('active', 'removed')),
                    remove_mode TEXT CHECK(remove_mode IN ('no_entries', 'close_all')),
                    changed_generation INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (contour_id, instrument_key)
                );
                CREATE TABLE IF NOT EXISTS heartbeat (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    core_pid INTEGER NOT NULL,
                    core_version TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    beat_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running', 'stopped'))
                );
                CREATE TABLE IF NOT EXISTS history_runs (
                    run_id TEXT PRIMARY KEY,
                    directory TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    pid INTEGER,
                    exit_code INTEGER,
                    reason TEXT,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    sync_json TEXT
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
            return _desired(conn, conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())

    # ── Пульс ядра (add-contour-recovery) ──

    def beat(self, pid: int, version: str, started_at: str) -> Heartbeat:
        """Удар пульса: ядро живо; строка одна на базу, новое ядро её перезаписывает."""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO heartbeat(id,core_pid,core_version,started_at,beat_at,state)
                VALUES(1,?,?,?,?,'running') ON CONFLICT(id) DO UPDATE SET core_pid=excluded.core_pid,
                core_version=excluded.core_version, started_at=excluded.started_at, beat_at=excluded.beat_at,
                state='running'""", (pid, version, started_at, now))
        return Heartbeat(pid, version, started_at, now, BEAT_RUNNING)

    def mark_stopped(self, pid: int) -> None:
        """Штатная остановка ядра: контуры этого ядра выходят после тика; чужую строку не трогает."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE heartbeat SET state='stopped', beat_at=? WHERE id=1 AND core_pid=?", (_now(), pid))

    def heartbeat(self) -> Heartbeat | None:
        with self._connect() as conn:
            return _heartbeat(conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone())

    # ── Прогоны истории (add-history-contour) ──

    def add_history_run(self, run_id: str, directory: str, request: dict) -> HistoryRun:
        """Новый прогон в очереди; ``run_id`` уникален — его подбирает очередь."""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO history_runs(run_id,directory,request_json,state,accepted_at)
                VALUES(?,?,?,?,?)""", (run_id, directory, json.dumps(request, ensure_ascii=False), RUN_QUEUED, now))
        return HistoryRun(run_id, directory, dict(request), RUN_QUEUED, now)

    def update_history_run(self, run_id: str, **fields) -> HistoryRun | None:
        """Поля прогона: ``state``, времена, ``pid``, ``exit_code``, ``reason``, ``stop_requested``, ``market_data_sync``."""
        columns = {"market_data_sync": "sync_json"}
        values = {columns.get(name, name): (json.dumps(value, ensure_ascii=False, default=str)
                                            if name == "market_data_sync" and value is not None
                                            else int(value) if name == "stop_requested" else value)
                  for name, value in fields.items()}
        with self._lock, self._connect() as conn:
            if values:
                conn.execute(f"UPDATE history_runs SET {', '.join(f'{name}=?' for name in values)} WHERE run_id=?",
                             (*values.values(), run_id))
            return _history_run(conn.execute("SELECT * FROM history_runs WHERE run_id=?", (run_id,)).fetchone())

    def history_run(self, run_id: str) -> HistoryRun | None:
        with self._connect() as conn:
            return _history_run(conn.execute("SELECT * FROM history_runs WHERE run_id=?", (run_id,)).fetchone())

    def history_runs(self, *, active: bool | None = None, limit: int | None = None) -> list[HistoryRun]:
        """Прогоны по времени приёма: ``active=True`` — незавершённые (старые первыми), ``False`` — завершённые (новые первыми)."""
        marks = ",".join("?" * len(RUN_ACTIVE))
        if active is None:
            sql, args = "SELECT * FROM history_runs ORDER BY accepted_at DESC, run_id DESC", ()
        elif active:
            sql, args = f"SELECT * FROM history_runs WHERE state IN ({marks}) ORDER BY accepted_at, run_id", RUN_ACTIVE
        else:
            sql = f"SELECT * FROM history_runs WHERE state NOT IN ({marks}) ORDER BY accepted_at DESC, run_id DESC"
            args = RUN_ACTIVE
        if limit is not None:
            sql, args = sql + " LIMIT ?", (*args, limit)
        with self._connect() as conn:
            return [_history_run(row) for row in conn.execute(sql, args).fetchall()]

    def record_launch(self, contour_id: str, kind: str, instruments, *, source: str = "", command: str = "",
                      payload: dict | None = None, members=None, expected: int | None = None) -> int:
        """Запуск: желаемое «работает» новым поколением и выбранный состав; возвращает поколение.

        ``members`` — участники ``Member`` с карточками контрактов; без них
        состав собирается из карточек ``instruments``. Прежний состав заменяется.
        ``expected`` — поколение из ``If-Match``: не совпало — ``RevisionChanged``.
        """
        instruments = list(instruments)
        if members is None:
            members = [Member(_member_key(item), item) for item in instruments]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                self._expect(conn, contour_id, "launch", payload if payload is not None else {"instruments": instruments},
                             expected, now)
                generation = self._generation(conn, contour_id) + 1
                conn.execute("""INSERT INTO contours(contour_id,kind,desired,stop_mode,generation,instruments_json,
                    source,command,updated_at) VALUES(?,?,'running',NULL,?,?,?,?,?)
                    ON CONFLICT(contour_id) DO UPDATE SET kind=excluded.kind, desired='running', stop_mode=NULL,
                    generation=excluded.generation, instruments_json=excluded.instruments_json,
                    source=excluded.source, command=excluded.command, updated_at=excluded.updated_at""",
                    (contour_id, kind, generation, json.dumps(instruments, ensure_ascii=False), source, command, now))
                conn.execute("DELETE FROM contour_members WHERE contour_id=?", (contour_id,))
                for member in members:
                    self._put_member(conn, contour_id, member, generation, now)
                self._log(conn, contour_id, "launch", payload if payload is not None else {"instruments": instruments},
                          generation, ACCEPTED, None, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return generation

    def record_stop(self, contour_id: str, mode: str, expected: int | None = None) -> StopOutcome:
        """Остановка: новое поколение; повтор того же режима — ``ignored``; ``close_all`` → ``no_entries`` — отказ."""
        if mode not in STOP_MODES:
            raise ValueError(f"Неизвестный режим остановки: {mode!r}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                self._expect(conn, contour_id, "stop", {"mode": mode}, expected, now)
                current = _desired(conn, conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone())
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

    def record_membership(self, contour_id: str, *, add=(), remove=(), mode: str = NO_ENTRIES,
                          payload: dict | None = None, expected: int | None = None) -> MembershipOutcome:
        """Изменение состава одной транзакцией с одним поколением.

        ``add`` — участники ``Member``: новые или возвращаемые из снятия;
        ``remove`` — ключи участников. Ничего не меняется — ``ignored``;
        ``no_entries`` поверх ``close_all`` — ``conflict`` для всей команды.
        """
        if mode not in STOP_MODES:
            raise ValueError(f"Неизвестный режим снятия: {mode!r}")
        add, remove = list(add), list(remove)
        payload = payload if payload is not None else {"add": [m.key for m in add], "remove": remove, "mode": mode}
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                self._expect(conn, contour_id, "remove" if remove else "add", payload, expected, now)
                current = _desired(conn, conn.execute("SELECT * FROM contours WHERE contour_id=?",
                                                      (contour_id,)).fetchone())
                outcome = self._membership(conn, contour_id, current, add, remove, mode, now)
                self._log(conn, contour_id, "remove" if remove else "add", payload, outcome.generation or None,
                          outcome.outcome, outcome.reason or None, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return outcome

    def _membership(self, conn, contour_id, current, add, remove, mode, now) -> MembershipOutcome:
        if current is None or not current.running:
            return MembershipOutcome(CONFLICT, 0 if current is None else current.generation,
                                     "контур не работает" if current is None else "контур останавливается")
        members = {member.key: member for member in current.members}
        if not members and current.instruments:
            # Контур запущен до add-contour-membership: состав — из карточек запуска.
            for item in current.instruments:
                member = Member(_member_key(item), item, changed_generation=current.generation)
                self._put_member(conn, contour_id, member, current.generation, now)
                members[member.key] = member
        changes = []
        for member in add:
            old = members.get(member.key)
            if old is None or not old.active:
                changes.append(Member(member.key, member.instrument, member.chosen or (old.chosen if old else None),
                                      member.contract or (old.contract if old else None)))
        for key in remove:
            old = members.get(key)
            if old is None:
                return MembershipOutcome(CONFLICT, current.generation, f"{key} нет в составе")
            if old.active or (old.remove_mode == NO_ENTRIES and mode == CLOSE_ALL):
                changes.append(Member(old.key, old.instrument, old.chosen, old.contract, False, mode))
            elif old.remove_mode == CLOSE_ALL and mode == NO_ENTRIES:
                return MembershipOutcome(CONFLICT, current.generation,
                                         f"{old.name} уже закрывается, режим без входов не вернуть")
        if not changes:
            return MembershipOutcome(IGNORED, current.generation)
        generation = current.generation + 1
        for member in changes:
            self._put_member(conn, contour_id, member, generation, now)
        conn.execute("UPDATE contours SET generation=?, updated_at=? WHERE contour_id=?", (generation, now, contour_id))
        return MembershipOutcome(ACCEPTED, generation)

    def record_bindings(self, contour_id: str, kind: str, *, add=(), remove=(), mode: str = NO_ENTRIES,
                        payload: dict | None = None, expected: int | None = None) -> MembershipOutcome:
        """Состав контура на брокере (add-sandbox-contour): работает и без запущенного процесса.

        Контура ещё нет — заводится строка «остановлен» без режима: состав ждёт
        первого запуска. Правила добавления и снятия — как у ``record_membership``.
        """
        if mode not in STOP_MODES:
            raise ValueError(f"Неизвестный режим снятия: {mode!r}")
        add, remove = list(add), list(remove)
        payload = payload if payload is not None else {"add": [m.key for m in add], "remove": remove, "mode": mode}
        action = "remove" if remove else "add"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                self._expect(conn, contour_id, action, payload, expected, now)
                row = conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone()
                if row is None:
                    conn.execute("""INSERT INTO contours(contour_id,kind,desired,stop_mode,generation,instruments_json,
                        source,command,updated_at) VALUES(?,?,'stopped',NULL,0,'[]',NULL,NULL,?)""",
                                 (contour_id, kind, now))
                    row = conn.execute("SELECT * FROM contours WHERE contour_id=?", (contour_id,)).fetchone()
                current = _desired(conn, row)
                members = {member.key: member for member in current.members}
                changes, outcome = [], None
                for member in add:
                    old = members.get(member.key)
                    if old is None or not old.active:
                        changes.append(Member(member.key, member.instrument))
                for key in remove:
                    old = members.get(key)
                    if old is None:
                        outcome = MembershipOutcome(CONFLICT, current.generation, f"{key} нет в составе")
                        break
                    if old.active or (old.remove_mode == NO_ENTRIES and mode == CLOSE_ALL):
                        changes.append(Member(old.key, old.instrument, old.chosen, old.contract, False, mode))
                if outcome is None and not changes:
                    outcome = MembershipOutcome(IGNORED, current.generation)
                if outcome is None:
                    outcome = MembershipOutcome(ACCEPTED, current.generation + 1)
                    for member in changes:
                        self._put_member(conn, contour_id, member, outcome.generation, now)
                    conn.execute("UPDATE contours SET generation=?, updated_at=? WHERE contour_id=?",
                                 (outcome.generation, now, contour_id))
                self._log(conn, contour_id, action, payload, outcome.generation or None, outcome.outcome,
                          outcome.reason or None, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return outcome

    def attach_contracts(self, contour_id: str, contracts: dict) -> None:
        """Карточки контрактов участникам состава без нового поколения: ключ участника → ``contracts_payload``."""
        now = _now()
        with self._lock, self._connect() as conn:
            for key, contract in contracts.items():
                conn.execute("UPDATE contour_members SET contract_json=?, updated_at=? WHERE contour_id=? AND instrument_key=?",
                             (json.dumps(contract, ensure_ascii=False, default=str), now, contour_id, key))

    def _expect(self, conn, contour_id, action, payload, expected, now) -> None:
        """Сверка ``If-Match`` внутри транзакции записи: не совпало — запись ``conflict`` и ``RevisionChanged``."""
        if expected is None:
            return
        current = self._generation(conn, contour_id)
        if current != expected:
            error = RevisionChanged(expected, current)
            self._log(conn, contour_id, action, payload, current or None, CONFLICT, str(error), now)
            conn.commit()
            raise error

    @staticmethod
    def _put_member(conn, contour_id, member: Member, generation: int, now: str) -> None:
        conn.execute("""INSERT INTO contour_members(contour_id,instrument_key,instrument_json,chosen_json,contract_json,
            desired,remove_mode,changed_generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(contour_id,instrument_key) DO UPDATE SET instrument_json=excluded.instrument_json,
            chosen_json=excluded.chosen_json, contract_json=excluded.contract_json, desired=excluded.desired,
            remove_mode=excluded.remove_mode, changed_generation=excluded.changed_generation,
            updated_at=excluded.updated_at""",
            (contour_id, member.key, json.dumps(member.instrument, ensure_ascii=False),
             None if member.chosen is None else json.dumps(member.chosen, ensure_ascii=False),
             None if member.contract is None else json.dumps(member.contract, ensure_ascii=False, default=str),
             "active" if member.active else "removed", member.remove_mode, generation, now))

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
