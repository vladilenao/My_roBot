"""Команды эмуляции из локального API и её состояние (rework-contour-api).

Поток HTTP только кладёт запрос в очередь и ждёт ответа — разрешение
инструментов, сборка и запуск торговли остаются в главном потоке робота.
Прогоны истории уходят в очередь прогонов ``HistoryRuns``, которая не
зависит от готовности ядра.

Внутри робота — желаемое, фактическое и поколение (``core.sqlite3``,
``status.json``); ответы API называют их ``target``, ``current`` и
``revision``. Перевод — здесь, в ``emulation()`` и ``members()``.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.bot import control_views as views
from src.bot.launch import (
    HISTORY_DEFAULT_PAUSE, MODE_EMULATION, MODE_HISTORY, SOURCE_API, Chosen, LaunchError, LaunchRequest, Rejection,
    RunConfigurationError, parse_instrument_tokens, parse_moment, validate_range,
)

STATE_WAITING = "waiting_command"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
CONTOUR_ID = "emulation"
REPLY_TIMEOUT_SECS = 60.0
TARGET_MODES = (views.MODE_ON, views.NO_ENTRIES, views.CLOSE_ALL)
STOP_MODES = (views.NO_ENTRIES, views.CLOSE_ALL)
_HISTORY_FIELDS = {"from", "to", "pause", "instruments"}
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


def parse_target(body, expected: int | None = None) -> tuple[str, LaunchRequest | None]:
    """Тело ``PUT /api/v1/emulation/target`` → ``(режим, запуск)``; запуск — только у ``on``."""
    message = "Тело — {\"mode\": \"on\", \"instruments\": [...]}, {\"mode\": \"no_entries\"} или {\"mode\": \"close_all\"}."
    if not isinstance(body, dict) or body.get("mode") not in TARGET_MODES:
        raise BadCommand("INVALID_REQUEST", message)
    mode = body["mode"]
    if mode != views.MODE_ON and "instruments" in body:
        raise BadCommand("INVALID_REQUEST", "instruments передаётся только с mode = on.")
    unknown = set(body) - {"mode", "instruments"}
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    if mode != views.MODE_ON:
        return mode, None
    return mode, LaunchRequest(MODE_EMULATION, SOURCE_API, _tokens(body.get("instruments")),
                               expected_revision=expected)


def parse_history(body) -> LaunchRequest:
    """Тело ``POST /api/v1/history`` → ``LaunchRequest`` прогона истории, иначе ``BadCommand``."""
    if not isinstance(body, dict):
        raise BadCommand("INVALID_REQUEST", "Тело прогона — JSON-объект с полями from, to, pause и instruments.")
    unknown = set(body) - _HISTORY_FIELDS
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    tokens = _tokens(body.get("instruments"))
    start, end = body.get("from"), body.get("to")
    if not isinstance(start, str) or not isinstance(end, str):
        raise BadCommand("INVALID_REQUEST", "from и to обязательны: строки ГГГГ-ММ-ДД ЧЧ:ММ (UTC).")
    pause = body.get("pause", HISTORY_DEFAULT_PAUSE)
    if isinstance(pause, bool) or not isinstance(pause, (int, float)) or pause < 0:
        raise BadCommand("INVALID_REQUEST", "pause — число секунд не меньше 0.")
    try:
        start, end, pause = validate_range(parse_moment(start), parse_moment(end), pause)
    except (ValueError, RunConfigurationError) as exc:
        raise BadCommand("INVALID_REQUEST", str(exc)) from None
    return LaunchRequest(MODE_HISTORY, SOURCE_API, tokens, start=start, end=end, pause=pause)


def _tokens(raw) -> tuple[str, ...]:
    if raw is None:
        tokens = parse_instrument_tokens(None)
    else:
        if not _names(raw):
            raise BadCommand("INVALID_REQUEST", "instruments — непустой список непустых строк.")
        try:
            tokens = parse_instrument_tokens(raw)
        except LaunchError as exc:
            raise BadCommand("INVALID_REQUEST", str(exc)) from None
    return tokens


def _names(raw) -> bool:
    return isinstance(raw, list) and bool(raw) and all(isinstance(item, str) and item.strip() for item in raw)


@dataclass
class MembershipCommand:
    """Команда состава эмуляции: ровно одно из ``add`` / ``remove``; ``expected`` — ревизия из ``If-Match``."""

    add: tuple = ()
    remove: tuple = ()
    mode: str = views.NO_ENTRIES
    expected: int | None = None
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    payload: dict | None = None


def parse_add(body, expected: int | None = None) -> MembershipCommand:
    """Тело ``POST /api/v1/emulation/instruments`` → команда или ``BadCommand``."""
    if not isinstance(body, dict) or set(body) != {"instruments"} or not _names(body["instruments"]):
        raise BadCommand("INVALID_REQUEST", "Тело — {\"instruments\": [\"NG-12.26\", \"SBER\"]}.")
    return MembershipCommand(add=tuple(dict.fromkeys(item.strip() for item in body["instruments"])), expected=expected)


def parse_remove(body, expected: int | None = None) -> MembershipCommand:
    """Тело ``POST /api/v1/emulation/instruments/remove`` → команда или ``BadCommand``."""
    message = "Тело — {\"instruments\": [...], \"mode\": \"no_entries\" | \"close_all\"}."
    if not isinstance(body, dict) or "instruments" not in body or set(body) - {"instruments", "mode"}:
        raise BadCommand("INVALID_REQUEST", message)
    mode = body.get("mode", views.NO_ENTRIES)
    if mode not in STOP_MODES or not _names(body["instruments"]):
        raise BadCommand("INVALID_REQUEST", message)
    return MembershipCommand(remove=tuple(dict.fromkeys(item.strip() for item in body["instruments"])), mode=mode,
                             expected=expected)


def _chosen_payload(member) -> dict:
    """Как участник назван при разрешении: токен, имя, тип и дни до экспирации."""
    chosen = member.chosen or {}
    return {"token": chosen.get("token") or member.instrument.get("ticker") or member.key,
            "name": member.name, "instrument_type": member.instrument.get("instrument_type"),
            "days_to_expiry": chosen.get("days_to_expiry")}


def launch_request(command: str | None) -> dict | None:
    """Эквивалентная команда запуска объектом; строка прежней версии без списка инструментов — ``None``."""
    try:
        payload = json.loads(command) if command else None
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
        return None
    return {"mode": views.MODE_ON, "instruments": [str(item) for item in payload["instruments"]]}


def tail_lines(path: Path, count: int) -> str:
    """Последние ``count`` строк файла журнала; файла нет — пустая строка."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return "".join(deque(stream, maxlen=count))
    except FileNotFoundError:
        return ""


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


class LaunchControl:
    """Состояние контура эмуляции процесса робота и очередь команды запуска."""

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
        # Процесс контура (add-contour-process): pid, пока жив; код и время выхода — после.
        self._pid: int | None = None
        self._exit_code: int | None = None
        self._exited_at: datetime | None = None
        # Желаемое и журнал команд — в core.sqlite3; фактическое — status.json контура (add-control-api).
        self._core_state = core_state
        self._contour_dir = None if contour_dir is None else Path(contour_dir)
        self._log_file = log_file
        self._launched = False
        # Запуск командой уже записан в core.sqlite3 (rework-contour-api): ядро не пишет его второй раз.
        self._recorded = False
        self._membership: queue.Queue[MembershipCommand] = queue.Queue()
        # Как поднят процесс этим ядром: autostart / adopted / None (add-contour-recovery).
        self._recovery: str | None = None
        # add-history-contour: очередь прогонов истории (HistoryRuns), состояние ядра —
        # функция без аргументов → ``GET /api/v1/core``, и проверка папки контура — pid чужого процесса или None.
        self.history = None
        self.core_status = None
        self.contour_busy = None

    # --- представление для API ------------------------------------------------
    def snapshot(self) -> dict:
        """Внутренний снимок для ядра и консоли: состояние, источник, команда, процесс."""
        desired = self._desired()
        with self._lock:
            return {"state": self._state_of(desired), "source": self._source, "command": self._command,
                    "pid": self._pid, "exit_code": self._exit_code, "recovery": self._recovery}

    def _state_of(self, desired) -> str:
        """Общее состояние; вызывается под ``self._lock``."""
        if self._pid is not None and desired is not None and not desired.running:
            return f"stopping_{desired.stop_mode}"
        return self._state

    def emulation(self) -> dict:
        """``GET /api/v1/emulation``: общее, целевое и текущее состояние, запуск, процесс, состав, ссылки, действия."""
        desired = self._desired()
        ready = self._core_not_ready() is None
        with self._lock:
            state = self._state_of(desired)
            actions = self._actions_with(desired, ready)
            launched = self._launched
            rejections = [{"instrument": item.token, "reason": item.reason} for item in self._rejections]
            launch = None
            if self._source is not None or rejections or desired is not None:
                launch = {"source": self._source or (desired.source if desired is not None else None) or None,
                          "recovery": self._recovery, "started_at": _iso(self._started_at),
                          "request": launch_request(self._command or (desired.command if desired else None)),
                          "rejections": rejections}
            process = None if not launched else {
                "pid": self._pid, "exit_code": self._exit_code, "exited_at": _iso(self._exited_at),
                "dir": self._dir_name()}
            chosen = list(self._chosen)
        # Снимок прошлого запуска ядра не выдаётся за текущее состояние этого.
        status = self._status() if launched else None
        current = None if status is None else {
            "state": status.get("state"), "applied_revision": status.get("applied_generation"),
            "open_positions": status.get("open_positions"), "pending_entries": status.get("pending_entries"),
            "last_tick_at": status.get("last_tick_at"), "core_link": status.get("core_link")}
        return {
            "state": state,
            "target": self._target(desired),
            "current": current,
            "launch": launch,
            "process": process,
            "instruments": self._member_views(desired, status, chosen, "add-instruments" in actions),
            "_links": views.links(self=views.EMULATION, target=views.EMULATION_TARGET,
                                  instruments=views.EMULATION_INSTRUMENTS, log=views.EMULATION_LOG),
            "_actions": views.emulation_action_list(actions),
        }

    def _actions_with(self, desired, ready: bool) -> tuple[str, ...]:
        """Допустимые команды эмуляции — для ``_actions`` и для проверки команд; вызывается под ``self._lock``."""
        idle = self._state in (STATE_WAITING, STATE_STOPPED, STATE_FAILED) and self._pid is None
        return views.emulation_actions(
            alive=self._pid is not None and self._core_state is not None,
            mode=None if desired is None else (views.MODE_ON if desired.running else desired.stop_mode),
            can_start=idle and self._accepting and self._pending is None and ready)

    @staticmethod
    def _target(desired) -> dict | None:
        if desired is None:
            return None
        return {"mode": views.MODE_ON if desired.running else desired.stop_mode, "revision": desired.generation}

    def target(self) -> dict | None:
        """``GET /api/v1/emulation/target``; ``None`` — эмуляцию ни разу не запускали."""
        return self._target(self._desired())

    def revision(self) -> int | None:
        desired = self._desired()
        return None if desired is None else desired.generation

    def _dir_name(self) -> str:
        if self._contour_dir is None:
            return f"contours/{CONTOUR_ID}"
        return f"{self._contour_dir.parent.name}/{self._contour_dir.name}"

    def _member_views(self, desired, status, chosen, changeable: bool) -> list[dict]:
        members = () if desired is None else desired.members
        current = {item.get("key"): item.get("state") for item in (status or {}).get("members") or ()}
        if members:
            # Текущий целевой состав (add-contour-membership), а не состав запуска.
            items = [(member.name, member.instrument.get("instrument_type"), (member.chosen or {}).get("days_to_expiry"),
                      member.active, member.remove_mode, current.get(member.key)) for member in members]
        else:
            items = [(item.name, item.instrument_type, item.days_to_expiry, True, None, None) for item in chosen]
        return [{
            "name": name, "instrument_type": kind, "days_to_expiry": days,
            "target": "active" if active else "removed", "remove_mode": remove_mode, "current": state,
            "_links": views.links(self=views.member_href(name)),
            "_actions": views.member_action_list(name, views.member_actions(
                changeable=changeable, active=active, remove_mode=remove_mode)),
        } for name, kind, days, active, remove_mode, state in items]

    def members(self, revision: int | None = -1) -> dict:
        """``GET /api/v1/emulation/instruments`` и ответ команд состава; ``revision=None`` — итог ещё не известен."""
        view = self.emulation()
        if revision == -1:
            revision = None if view["target"] is None else view["target"]["revision"]
        return {"revision": revision, "items": view["instruments"],
                "_links": views.links(self=views.EMULATION_INSTRUMENTS, emulation=views.EMULATION),
                "_actions": [action for action in view["_actions"] if action["name"] == "add-instruments"]}

    def member(self, name: str) -> dict | None:
        wanted = name.strip().upper()
        return next((item for item in self.emulation()["instruments"] if item["name"].upper() == wanted), None)

    def summary(self) -> dict:
        """Сводка эмуляции для точки входа ``GET /api/v1``."""
        view = self.emulation()
        return {"state": view["state"],
                "instruments": sum(item["target"] == "active" for item in view["instruments"]),
                "open_positions": (view["current"] or {}).get("open_positions")}

    def chosen_payloads(self) -> list[dict]:
        """Как инструменты последнего запуска названы при разрешении — для участников состава."""
        with self._lock:
            return [item.payload() for item in self._chosen]

    def _core_not_ready(self) -> dict | None:
        """Тело ``409 CORE_NOT_READY``, пока ядро стартует или повторяет старт; ``None`` — ядро готово."""
        if self.core_status is None:
            return None
        core = self.core_status()
        if core.get("state", "ready") == "ready":
            return None
        return {"code": "CORE_NOT_READY", "message": "Ядро не готово: эмуляция запустится, когда ядро поднимется.",
                "core": core}

    def _desired(self):
        return None if self._core_state is None else self._core_state.desired(CONTOUR_ID)

    def _status(self) -> dict | None:
        if self._contour_dir is None:
            return None
        from src.assembly.contour_control import STATUS_FILE, read_status
        return read_status(self._contour_dir / STATUS_FILE)

    def _revision_changed(self, expected: int | None, action: str, payload: dict) -> dict | None:
        """Быстрая сверка ``If-Match`` до разрешения инструментов; атомарная — при записи в ``core.sqlite3``."""
        if expected is None:
            return None
        current = self.revision() or 0
        if current == expected:
            return None
        if self._core_state is not None:
            self._core_state.record_command(CONTOUR_ID, action, payload, "conflict",
                                            f"Ожидалась ревизия {expected}, текущая {current}.")
        return _revision_problem(expected, current)

    # --- команды ----------------------------------------------------------------
    def put_target(self, mode: str, request: LaunchRequest | None, expected: int | None = None) -> tuple[int, dict]:
        """``PUT /api/v1/emulation/target``: ``on`` — запуск, ``no_entries`` / ``close_all`` — остановка."""
        if mode == views.MODE_ON:
            return self.submit(request)
        return self.stop(mode, expected)

    def stop(self, mode: str, expected: int | None = None) -> tuple[int, dict]:
        """Остановка: целевое «остановлен» новым поколением; применит контур на границе тика."""
        from src.assembly.core_state import RevisionChanged

        with self._lock:
            alive = self._pid is not None
        if not alive or self._core_state is None:
            if self._core_state is not None:
                self._core_state.record_command(CONTOUR_ID, "stop", {"mode": mode}, "conflict", "контур не работает")
            return 409, {"code": "NOT_RUNNING", "message": "Эмуляция не работает: останавливать нечего."}
        try:
            outcome = self._core_state.record_stop(CONTOUR_ID, mode, expected)
        except RevisionChanged as exc:
            return 412, _revision_problem(exc.expected, exc.current)
        if outcome.outcome == "conflict":
            return 409, {"code": "MODE_CONFLICT", "message": f"Остановка не принята: {outcome.reason}."}
        return (202 if outcome.outcome == "accepted" else 200), self.emulation()

    def change_membership(self, command: MembershipCommand) -> tuple[int, dict]:
        """Команда состава из HTTP-потока: разбирает главный поток ядра, ответ — не дольше таймаута."""
        desired = self._desired()
        with self._lock:
            alive = self._pid is not None
        if not alive:
            return 409, {"code": "NOT_RUNNING", "message": "Эмуляция не работает: состав менять нечему."}
        if desired is not None and not desired.running:
            return 409, {"code": "STOPPING", "message": "Эмуляция останавливается: состав не меняется."}
        payload = {"add": list(command.add)} if command.add else {"remove": list(command.remove), "mode": command.mode}
        changed = self._revision_changed(command.expected, "add" if command.add else "remove", payload)
        if changed is not None:
            return 412, changed
        self._membership.put(command)
        if not command.done.wait(self._reply_timeout):
            return 202, self.members(revision=None)
        if command.status in (200, 202):
            return command.status, self.members(revision=command.payload.get("revision"))
        return command.status, command.payload

    def next_membership(self, timeout: float):
        """Главный поток ядра: следующая команда состава или ``None`` за ``timeout``."""
        try:
            return self._membership.get(timeout=timeout)
        except queue.Empty:
            return None

    def answer_membership(self, command: MembershipCommand, status: int, payload: dict) -> None:
        command.status, command.payload = status, payload
        command.done.set()

    def log_tail(self, count: int) -> str:
        """Последние строки журнала процесса эмуляции; секреты в нём уже замаскированы."""
        return "" if self._contour_dir is None else tail_lines(self._contour_dir / self._log_file, count)

    def submit(self, request: LaunchRequest) -> tuple[int, dict]:
        """Передать команду запуска главному потоку и дождаться ответа не дольше таймаута.

        Команда принимается, пока ядро ждёт её, в том числе после остановки или
        падения контура (повторный запуск, add-control-api).
        """
        launch_payload = {"instruments": list(request.instruments)}
        not_ready = self._core_not_ready()
        if not_ready is not None:
            return 409, not_ready
        busy = None if self.contour_busy is None else self.contour_busy()
        if busy is not None:
            return 409, {"code": "CONTOUR_DIR_BUSY",
                         "message": f"Папку контура держит процесс pid {busy}, который не подтверждает себя контуром "
                                    "робота. Завершите его; команда проверит папку заново.",
                         "pid": busy}
        changed = self._revision_changed(request.expected_revision, "launch", launch_payload)
        if changed is not None:
            return 412, changed
        desired = self._desired()
        with self._lock:
            allowed = "start" in self._actions_with(desired, True)
            stopping = self._pid is not None and desired is not None and not desired.running
            if allowed:
                pending = _Pending(request)
                self._pending = pending
                self._state = STATE_STARTING
                self._source = SOURCE_API
        if not allowed:
            if self._core_state is not None:
                self._core_state.record_command(CONTOUR_ID, "launch", launch_payload, "conflict",
                                                "контур останавливается" if stopping else "контур работает")
            if stopping:
                return 409, {"code": "STOPPING",
                             "message": "Эмуляция останавливается и доживает сделки; дождитесь остановки "
                                        "или передайте mode = close_all."}
            return 409, {"code": "ALREADY_RUNNING",
                         "message": "Эмуляция уже запускается или торгует; состав меняется через "
                                    "/api/v1/emulation/instruments."}
        self._queue.put(pending)
        if pending.done.wait(self._reply_timeout) and pending.status != 202:
            return pending.status, pending.payload
        return 202, self.emulation()

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

    def mark_launched(self, source: str, chosen: list[Chosen], command: str, pending: _Pending | None = None, *,
                      instruments=None) -> bool:
        """Инструменты приняты: контур запускается (``starting``) до первого подтверждения курсора.

        С ``instruments`` целевое «работает» и состав записываются в
        ``core.sqlite3`` сразу (rework-contour-api): ответ команды несёт новую
        ревизию. ``If-Match`` команды сверяется в той же транзакции; не совпал —
        ответ ``412``, робот ждёт следующую команду, возвращается ``False``.
        """
        from src.assembly.core_state import RevisionChanged

        if instruments is not None and self._core_state is not None:
            from src.assembly.contracts import instrument_payload
            from src.assembly.core import _member
            from src.instruments import normalize_instrument

            normalized = [normalize_instrument(item) for item in instruments]
            chosen_payloads = [item.payload() for item in chosen]
            try:
                self._core_state.record_launch(
                    CONTOUR_ID, MODE_EMULATION, [instrument_payload(item) for item in normalized], source=source,
                    command=command, members=[_member(item, {}, chosen_payloads) for item in normalized],
                    expected=None if pending is None else pending.request.expected_revision)
            except RevisionChanged as exc:
                with self._lock:
                    self._state = STATE_WAITING
                    self._source = None
                    self._pending = None
                if pending is not None:
                    pending.status, pending.payload = 412, _revision_problem(exc.expected, exc.current)
                    pending.done.set()
                return False
        with self._lock:
            self._state = STATE_STARTING
            self._source = source
            self._chosen = list(chosen)
            self._command = command
            self._started_at = self._clock()
            self._rejections = []
            self._accepting = False
            self._pending = None
            self._recovery = None
            self._recorded = instruments is not None and self._core_state is not None
        if pending is not None:
            pending.status = 202
            pending.done.set()
        return True

    def launch_recorded(self) -> bool:
        """Запуск этой командой уже записан в ``core.sqlite3``: ядро берёт поколение оттуда."""
        with self._lock:
            return self._recorded

    def mark_recovered(self, recovery: str, desired) -> None:
        """Процесс поднят без команды (``autostart``) или подхвачен живым (``adopted``): источник — из желаемого."""
        chosen = [] if desired is None else [
            Chosen(item["token"], item["name"], item["instrument_type"], item.get("days_to_expiry"))
            for item in (_chosen_payload(member) for member in desired.members if member.active)]
        with self._lock:
            self._state = STATE_STARTING
            self._source = None if desired is None else desired.source or None
            self._command = None if desired is None else desired.command or None
            self._chosen = chosen
            self._started_at = self._clock()
            self._rejections = []
            self._accepting = False
            self._recovery = recovery
            self._recorded = False

    def mark_process(self, pid: int) -> None:
        with self._lock:
            self._pid = pid
            self._exit_code = None
            self._exited_at = None
            self._launched = True
            self._recorded = False

    def mark_contour_running(self) -> None:
        """Контур первый раз подтвердил курсор ленты — торгует."""
        with self._lock:
            if self._state == STATE_STARTING and self._pid is not None:
                self._state = STATE_RUNNING

    def mark_exited(self, code: int | None, *, stopping: bool = False) -> None:
        """Процесс контура завершился: код 0 — ``stopped``, иначе ``failed`` (сам не перезапускается).

        ``None`` — подхваченный процесс вышел без отметки штатного выхода.
        """
        with self._lock:
            self._pid = None
            self._exit_code = code
            self._exited_at = self._clock()
            self._state = STATE_STOPPED if code == 0 else STATE_FAILED

    def mark_starting(self, source: str) -> None:
        with self._lock:
            self._state = STATE_STARTING
            self._source = source


def _revision_problem(expected: int, current: int) -> dict:
    return {"code": "REVISION_CHANGED", "message": f"Ожидалась ревизия {expected}, текущая {current}; перечитайте эмуляцию."}
