"""Команда запуска эмуляции из локального API и состояние контура.

Поток HTTP только кладёт запрос в очередь и ждёт ответа: разрешение
инструментов, сборка и запуск торговли остаются в главном потоке робота.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

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


class LaunchControl:
    """Состояние единственного контура процесса и очередь команды запуска."""

    def __init__(self, *, reply_timeout: float = REPLY_TIMEOUT_SECS, clock=None) -> None:
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

    # --- для API ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": CONTOUR_ID, "kind": MODE_EMULATION, "state": self._state, "source": self._source,
                "instruments": [item.payload() for item in self._chosen],
                "started_at": self._started_at.isoformat().replace("+00:00", "Z") if self._started_at else None,
                "command": self._command,
                "rejections": [{"token": item.token, "reason": item.reason} for item in self._rejections],
                "pid": self._pid,
                "exit_code": self._exit_code,
            }

    def submit(self, request: LaunchRequest) -> tuple[int, dict]:
        """Передать команду главному потоку и дождаться ответа не дольше таймаута."""
        with self._lock:
            if not self._accepting or self._state != STATE_WAITING or self._pending is not None:
                conflict = True
            else:
                conflict = False
                pending = _Pending(request)
                self._pending = pending
                self._state = STATE_STARTING
                self._source = SOURCE_API
        if conflict:
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
            self._state = STATE_WAITING

    def next_command(self, poll_secs: float = 0.5) -> _Pending:
        """Ждать команду; короткий опрос оставляет Ctrl+C рабочим."""
        while True:
            try:
                return self._queue.get(timeout=poll_secs)
            except queue.Empty:
                continue

    def reject(self, pending: _Pending, rejections: list[Rejection]) -> None:
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
