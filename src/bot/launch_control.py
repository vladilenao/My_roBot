"""Команда запуска эмуляции из локального API и состояние контура.

Поток HTTP только кладёт запрос в очередь и ждёт ответа: разрешение
инструментов, сборка и запуск торговли остаются в главном потоке робота.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.bot.launch import (
    MODE_EMULATION, SOURCE_API, Chosen, LaunchError, LaunchRequest, Rejection, parse_instrument_tokens,
)

STATE_WAITING = "waiting_command"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
CONTOUR_ID = "emulation"
REPLY_TIMEOUT_SECS = 60.0
_ALLOWED_FIELDS = {"kind", "instruments"}
STOP_MODES = ("no_entries", "close_all")
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 1000


class BadCommand(ValueError):
    """Тело команды неверно; ``code`` — машинный код ошибки API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Pending:
    request: LaunchRequest
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    payload: dict | None = None


def parse_command(body) -> LaunchRequest:
    """Тело ``POST /api/v1/contours`` → ``LaunchRequest`` или ``BadCommand``."""
    if not isinstance(body, dict):
        raise BadCommand("INVALID_REQUEST", "Тело команды — JSON-объект с полями kind и instruments.")
    unknown = set(body) - _ALLOWED_FIELDS
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    if body.get("kind") != MODE_EMULATION:
        raise BadCommand("UNSUPPORTED_KIND", "В этом процессе командой запускается только эмуляция.")
    raw = body.get("instruments")
    if raw is None:
        tokens = parse_instrument_tokens(None)
    else:
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item.strip() for item in raw):
            raise BadCommand("INVALID_REQUEST", "instruments — непустой список непустых строк.")
        try:
            tokens = parse_instrument_tokens(raw)
        except LaunchError as exc:
            raise BadCommand("INVALID_REQUEST", str(exc)) from None
    return LaunchRequest(MODE_EMULATION, SOURCE_API, tokens)


@dataclass
class MembershipCommand:
    """Команда состава контура (add-contour-membership): ровно одно из ``add`` / ``remove``."""

    add: tuple = ()
    remove: tuple = ()
    mode: str = "no_entries"
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    payload: dict | None = None


def parse_membership(body) -> MembershipCommand:
    """Тело ``POST /api/v1/contours/{id}/instruments`` → команда или ``BadCommand``."""
    message = "Тело — {\"add\": [...]} или {\"remove\": [...], \"mode\": \"no_entries\" | \"close_all\"}."
    if not isinstance(body, dict) or len({"add", "remove"} & set(body)) != 1:
        raise BadCommand("INVALID_REQUEST", message)
    allowed = {"add"} if "add" in body else {"remove", "mode"}
    tokens = body.get("add", body.get("remove"))
    mode = body.get("mode", "no_entries")
    if (set(body) - allowed or mode not in STOP_MODES or not isinstance(tokens, list) or not tokens
            or not all(isinstance(item, str) and item.strip() for item in tokens)):
        raise BadCommand("INVALID_REQUEST", message)
    tokens = tuple(dict.fromkeys(item.strip() for item in tokens))
    return MembershipCommand(add=tokens) if "add" in body else MembershipCommand(remove=tokens, mode=mode)


def parse_stop(body) -> str:
    """Тело ``POST /api/v1/contours/{id}/stop`` → режим остановки или ``BadCommand``."""
    if not isinstance(body, dict) or set(body) != {"mode"} or body["mode"] not in STOP_MODES:
        raise BadCommand("INVALID_REQUEST", "Тело остановки — {\"mode\": \"no_entries\"} или {\"mode\": \"close_all\"}.")
    return body["mode"]


def _chosen_payload(member) -> dict:
    """Участник состава в форме ``ContourInstrument``: как его назвали при разрешении."""
    chosen = member.chosen or {}
    return {"token": chosen.get("token") or member.instrument.get("ticker") or member.key,
            "name": member.name, "instrument_type": member.instrument.get("instrument_type"),
            "days_to_expiry": chosen.get("days_to_expiry")}


def tail_lines(path: Path, count: int) -> str:
    """Последние ``count`` строк файла журнала; файла нет — пустая строка."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return "".join(deque(stream, maxlen=count))
    except FileNotFoundError:
        return ""


class LaunchControl:
    """Состояние единственного контура процесса и очередь команды запуска."""

    def __init__(self, *, reply_timeout: float = REPLY_TIMEOUT_SECS, clock=None, core_state=None,
                 contour_dir: Path | None = None, log_file: str = "bot_debug.log") -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue[_Pending] = queue.Queue()
        self._reply_timeout = reply_timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._state = STATE_WAITING
        self._source: str | None = None
        self._chosen: list[Chosen] = []
        self._started_at: datetime | None = None
        self._command: str | None = None
        self._rejections: list[Rejection] = []
        self._accepting = False
        self._pending: _Pending | None = None
        # Процесс контура (add-contour-process): pid, пока жив; код выхода — после.
        self._pid: int | None = None
        self._exit_code: int | None = None
        # Желаемое и журнал команд — в core.sqlite3; фактическое — status.json контура (add-control-api).
        self._core_state = core_state
        self._contour_dir = None if contour_dir is None else Path(contour_dir)
        self._log_file = log_file
        self._launched = False
        self._membership: queue.Queue[MembershipCommand] = queue.Queue()

    # --- для API ----------------------------------------------------------
    def snapshot(self) -> dict:
        desired = self._desired()
        with self._lock:
            alive = self._pid is not None
            state = self._state
            if alive and desired is not None and not desired.running:
                state = f"stopping_{desired.stop_mode}"
            snapshot = {
                "id": CONTOUR_ID, "kind": MODE_EMULATION, "state": state, "source": self._source,
                "instruments": [item.payload() for item in self._chosen],
                "started_at": self._started_at.isoformat().replace("+00:00", "Z") if self._started_at else None,
                "command": self._command,
                "rejections": [{"token": item.token, "reason": item.reason} for item in self._rejections],
                "pid": self._pid,
                "exit_code": self._exit_code,
                "desired": None if desired is None else desired.payload(),
            }
            launched = self._launched
        # Снимок прошлого запуска ядра не выдаётся за фактическое этого.
        status = self._status() if launched else None
        snapshot["actual"] = None if status is None else {name: status.get(name) for name in (
            "state", "applied_generation", "open_positions", "pending_entries", "last_tick_at")}
        members = () if desired is None else desired.members
        if members:
            # Текущий желаемый состав (add-contour-membership), а не состав запуска.
            snapshot["instruments"] = [_chosen_payload(member) for member in members if member.active]
        actual = {item.get("key"): item.get("state") for item in (status or {}).get("members") or ()}
        snapshot["members"] = [{"name": member.name, "instrument_type": member.instrument.get("instrument_type"),
                                "desired": "active" if member.active else "removed",
                                "remove_mode": member.remove_mode, "actual": actual.get(member.key)}
                               for member in members]
        return snapshot

    def chosen_payloads(self) -> list[dict]:
        """Как инструменты последнего запуска названы при разрешении — для участников состава."""
        with self._lock:
            return [item.payload() for item in self._chosen]

    def contour(self, contour_id: str) -> dict | None:
        return self.snapshot() if contour_id == CONTOUR_ID else None

    def _desired(self):
        return None if self._core_state is None else self._core_state.desired(CONTOUR_ID)

    def _status(self) -> dict | None:
        if self._contour_dir is None:
            return None
        from src.assembly.contour_control import STATUS_FILE, read_status
        return read_status(self._contour_dir / STATUS_FILE)

    def stop(self, contour_id: str, mode: str) -> tuple[int, dict]:
        """Команда остановки: желаемое «остановлен» новым поколением; применит контур на границе тика."""
        if contour_id != CONTOUR_ID:
            return 404, {"code": "NOT_FOUND", "message": f"Контура {contour_id!r} нет."}
        with self._lock:
            alive = self._pid is not None
        if not alive or self._core_state is None:
            if self._core_state is not None:
                self._core_state.record_command(CONTOUR_ID, "stop", {"mode": mode}, "conflict", "контур не работает")
            return 409, {"code": "NOT_RUNNING", "message": "Контур не работает: останавливать нечего.",
                         "contour": self.snapshot()}
        outcome = self._core_state.record_stop(CONTOUR_ID, mode)
        if outcome.outcome == "conflict":
            return 409, {"code": "MODE_CONFLICT", "message": f"Остановка не принята: {outcome.reason}.",
                         "contour": self.snapshot()}
        status = 202 if outcome.outcome == "accepted" else 200
        return status, {"generation": outcome.generation, "contour": self.snapshot()}

    def change_membership(self, contour_id: str, command: MembershipCommand) -> tuple[int, dict]:
        """Команда состава из HTTP-потока: разбирает главный поток ядра, ответ — не дольше таймаута."""
        if contour_id != CONTOUR_ID:
            return 404, {"code": "NOT_FOUND", "message": f"Контура {contour_id!r} нет."}
        desired = self._desired()
        with self._lock:
            alive = self._pid is not None
        if not alive:
            return 409, {"code": "NOT_RUNNING", "message": "Контур не работает: состав менять нечему.",
                         "contour": self.snapshot()}
        if desired is not None and not desired.running:
            return 409, {"code": "STOPPING", "message": "Контур останавливается: состав не меняется.",
                         "contour": self.snapshot()}
        self._membership.put(command)
        if command.done.wait(self._reply_timeout):
            return command.status, command.payload
        return 202, {"generation": None, "contour": self.snapshot()}

    def next_membership(self, timeout: float):
        """Главный поток ядра: следующая команда состава или ``None`` за ``timeout``."""
        try:
            return self._membership.get(timeout=timeout)
        except queue.Empty:
            return None

    def answer_membership(self, command: MembershipCommand, status: int, payload: dict) -> None:
        command.status, command.payload = status, payload
        command.done.set()

    def log_tail(self, contour_id: str, count: int) -> str | None:
        """Последние строки журнала процесса контура; секреты в нём уже замаскированы."""
        if contour_id != CONTOUR_ID:
            return None
        return "" if self._contour_dir is None else tail_lines(self._contour_dir / self._log_file, count)

    def submit(self, request: LaunchRequest) -> tuple[int, dict]:
        """Передать команду главному потоку и дождаться ответа не дольше таймаута.

        Команда принимается, пока ядро ждёт её, в том числе после остановки или
        падения контура (повторный запуск, add-control-api).
        """
        desired = self._desired()
        with self._lock:
            idle = self._state in (STATE_WAITING, STATE_STOPPED, STATE_FAILED) and self._pid is None
            conflict = not self._accepting or not idle or self._pending is not None
            stopping = self._pid is not None and desired is not None and not desired.running
            if not conflict:
                pending = _Pending(request)
                self._pending = pending
                self._state = STATE_STARTING
                self._source = SOURCE_API
        if conflict:
            if self._core_state is not None:
                self._core_state.record_command(CONTOUR_ID, "launch", {"instruments": list(request.instruments)},
                                                "conflict", "контур останавливается" if stopping else "контур работает")
            if stopping:
                return 409, {"code": "STOPPING",
                             "message": "Контур останавливается и доживает сделки; дождитесь остановки "
                                        "или вызовите stop с close_all.",
                             "contour": self.snapshot()}
            return 409, {"code": "ALREADY_RUNNING",
                         "message": "Контур эмуляции уже запускается или торгует; набор инструментов не изменён.",
                         "contour": self.snapshot()}
        self._queue.put(pending)
        if pending.done.wait(self._reply_timeout):
            return pending.status, pending.payload
        return 202, self.snapshot()

    # --- для главного потока -------------------------------------------------
    def accept_commands(self) -> None:
        with self._lock:
            self._accepting = True
            # После выхода контура его итог остаётся виден до следующего запуска.
            if self._state not in (STATE_STOPPED, STATE_FAILED):
                self._state = STATE_WAITING

    def next_command(self, poll_secs: float = 0.5) -> _Pending:
        """Ждать команду; короткий опрос оставляет Ctrl+C рабочим."""
        while True:
            try:
                return self._queue.get(timeout=poll_secs)
            except queue.Empty:
                continue

    def reject(self, pending: _Pending, rejections: list[Rejection]) -> None:
        if self._core_state is not None:
            self._core_state.record_command(
                CONTOUR_ID, "launch", {"instruments": list(pending.request.instruments)}, "rejected",
                "; ".join(f"{item.token}: {item.reason}" for item in rejections))
        with self._lock:
            self._state = STATE_WAITING
            self._source = None
            self._rejections = list(rejections)
            self._pending = None
            pending.status = 422
            pending.payload = {"code": "INSTRUMENTS_REJECTED", "message": "Торговля не запущена.",
                               "rejections": [{"token": item.token, "reason": item.reason} for item in rejections]}
        pending.done.set()

    def mark_launched(self, source: str, chosen: list[Chosen], command: str, pending: _Pending | None = None) -> None:
        """Инструменты приняты: контур запускается (``starting``) до первого подтверждения курсора."""
        with self._lock:
            self._state = STATE_STARTING
            self._source = source
            self._chosen = list(chosen)
            self._command = command
            self._started_at = self._clock()
            self._rejections = []
            self._accepting = False
            self._pending = None
        if pending is not None:
            pending.status = 201
            pending.payload = self.snapshot()
            pending.done.set()

    def mark_process(self, pid: int) -> None:
        with self._lock:
            self._pid = pid
            self._exit_code = None
            self._launched = True

    def mark_contour_running(self) -> None:
        """Контур первый раз подтвердил курсор ленты — торгует."""
        with self._lock:
            if self._state == STATE_STARTING and self._pid is not None:
                self._state = STATE_RUNNING

    def mark_exited(self, code: int, *, stopping: bool = False) -> None:
        """Процесс контура завершился: код 0 — ``stopped``, иначе ``failed`` (сам не перезапускается)."""
        with self._lock:
            self._pid = None
            self._exit_code = code
            self._state = STATE_STOPPED if code == 0 else STATE_FAILED

    def mark_starting(self, source: str) -> None:
        with self._lock:
            self._state = STATE_STARTING
            self._source = source
