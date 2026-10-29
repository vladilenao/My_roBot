"""Ресурс API ``/api/v1/sandbox`` (add-sandbox-contour): состояние, команды, состав, сверка.

Команды пишут целевое состояние в ``core.sqlite3`` и, если нужно, запускают
процесс через надзирателя ядра. Текущее состояние — из ``status.json`` и
``broker.sqlite3`` контура, только на чтение. Слова API — слова оператора:
``target`` / ``current`` / ``revision``.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from pathlib import Path

from src.assembly.contour_control import STATUS_FILE, read_status
from src.assembly.core_state import ACCEPTED, CLOSE_ALL, CONFLICT, NO_ENTRIES, STOP_MODES, RevisionChanged
from src.bot import control_views as views
from src.bot.launch_control import BadCommand, tail_lines
from src.broker_contour.bindings import binding_catalog, binding_member, emulation_traded, unknown_bindings
from src.broker_contour.store import BROKER_DATABASE_FILE, read_last_report

SOURCE_API = "api"
MODES = (views.MODE_ON, NO_ENTRIES, CLOSE_ALL)


def _ids(raw, *, required: bool) -> tuple[str, ...]:
    if raw is None and not required:
        return ()
    if not isinstance(raw, list) or (required and not raw) or not all(isinstance(item, str) and item.strip()
                                                                       for item in raw):
        raise BadCommand("INVALID_REQUEST", "bindings — список непустых строк: id привязок из [strategies.*].")
    return tuple(dict.fromkeys(item.strip() for item in raw))


def parse_sandbox_target(body) -> tuple[str, tuple[str, ...] | None]:
    """Тело ``PUT /api/v1/sandbox/target`` → ``(режим, привязки)``; привязки — только у ``on``."""
    if not isinstance(body, dict) or body.get("mode") not in MODES:
        raise BadCommand("INVALID_REQUEST", "Тело — {\"mode\": \"on\", \"bindings\": [...]}, "
                                            "{\"mode\": \"no_entries\"} или {\"mode\": \"close_all\"}.")
    unknown = set(body) - {"mode", "bindings"}
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    if body["mode"] != views.MODE_ON:
        if "bindings" in body:
            raise BadCommand("INVALID_REQUEST", "bindings передаётся только с mode = on.")
        return body["mode"], None
    return views.MODE_ON, (_ids(body["bindings"], required=False) if "bindings" in body else None)


def parse_bindings(body, *, remove: bool) -> tuple[tuple[str, ...], str]:
    """Тело ``POST /api/v1/sandbox/bindings[/remove]`` → ``(привязки, режим снятия)``."""
    allowed = {"bindings", "mode"} if remove else {"bindings"}
    if not isinstance(body, dict) or "bindings" not in body or set(body) - allowed:
        raise BadCommand("INVALID_REQUEST", "Тело — {\"bindings\": [...]}" +
                         (", \"mode\": \"no_entries\" | \"close_all\"" if remove else "") + ".")
    mode = body.get("mode", NO_ENTRIES)
    if mode not in STOP_MODES:
        raise BadCommand("INVALID_REQUEST", "mode — no_entries или close_all.")
    return _ids(body["bindings"], required=True), mode


class SandboxControl:
    def __init__(self, *, kind: str, core_state, supervisor, share_strategies: dict, future_strategies: dict,
                 emulation_members=lambda: (), log_file: str = "bot_debug.log") -> None:
        self.kind = kind
        self._core_state = core_state
        self._supervisor = supervisor
        self._catalog = binding_catalog(share_strategies, future_strategies)
        self._emulation_members = emulation_members
        self._log_file = log_file
        self._lock = threading.Lock()
        self._rejections: list[dict] = []

    # --- чтение -----------------------------------------------------------
    @property
    def directory(self) -> Path:
        return self._supervisor.directory

    def _desired(self):
        return self._core_state.desired(self.kind)

    def _status(self) -> dict | None:
        return read_status(self.directory / STATUS_FILE)

    @staticmethod
    def _launched(desired) -> bool:
        return desired is not None and (desired.running or desired.stop_mode is not None)

    def _state(self, desired, status, process) -> str:
        if process["running"]:
            if desired is not None and not desired.running and desired.stop_mode:
                return f"stopping_{desired.stop_mode}"
            if status and status.get("pid") == process["pid"] and status.get("state") == "running":
                return "running"
            return "starting"
        if process["exited_at"] or process["exit_code"] is not None:
            return "stopped" if process["exit_code"] == 0 else "failed"
        if status and status.get("state") == "failed" and self._launched(desired):
            return "failed"
        return "waiting_command"

    def _target(self, desired) -> dict | None:
        if not self._launched(desired):
            return None
        return {"mode": views.MODE_ON if desired.running else desired.stop_mode, "revision": desired.generation}

    @staticmethod
    def _current(status) -> dict | None:
        if not status:
            return None
        state = status.get("state")
        return {
            "state": state if state in ("running", "stopped", "failed") else None,
            "applied_revision": status.get("applied_generation"), "account": status.get("account"),
            "capital": status.get("capital"), "positions": status.get("positions") or [],
            "orders": status.get("orders") or 0, "stop_orders": status.get("stop_orders") or 0,
            "frozen": status.get("frozen") or [], "last_reconcile_at": status.get("last_reconcile_at"),
            "account_link": status.get("account_link"), "core_link": status.get("core_link"),
            "reason": status.get("reason"),
        }

    def _launch(self, desired, process) -> dict | None:
        if not self._launched(desired) and not process["started_at"]:
            return None
        try:
            request = json.loads(desired.command) if desired is not None and desired.command else None
        except ValueError:
            request = None
        return {"source": SOURCE_API if desired is not None and desired.source == SOURCE_API else None,
                "recovery": process["recovery"], "started_at": process["started_at"], "request": request,
                "rejections": list(self._rejections)}

    def _member_view(self, member, status, running: bool) -> dict:
        card = member.instrument
        watched = running and status and any(item.get("key") == member.key for item in status.get("members") or ())
        return {"id": member.key, "instrument": card.get("instrument"), "strategy": card.get("strategy"),
                "timeframe": card.get("timeframe"), "target": "active" if member.active else "removed",
                "remove_mode": member.remove_mode, "current": "watching" if watched and member.active else None,
                "_actions": views.binding_action_list(member.key, member.active)}

    def view(self) -> dict:
        desired, status, process = self._desired(), self._status(), self._supervisor.snapshot()
        state = self._state(desired, status, process)
        target = self._target(desired)
        can_start = self._supervisor.ready and not process["running"]
        actions = views.sandbox_actions(alive=process["running"], mode=(target or {}).get("mode"),
                                        can_start=can_start)
        return {
            "state": state, "target": target, "current": self._current(status),
            "launch": self._launch(desired, process),
            "process": None if not process["started_at"] else {
                "pid": process["pid"], "exit_code": process["exit_code"], "exited_at": process["exited_at"],
                "dir": f"contours/{self.kind}"},
            "bindings": [self._member_view(member, status, process["running"])
                         for member in (desired.members if desired is not None else ())],
            "_links": views.links(self=views.SANDBOX, target=views.SANDBOX_TARGET, bindings=views.SANDBOX_BINDINGS,
                                  reconcile=views.SANDBOX_RECONCILE, log=views.SANDBOX_LOG),
            "_actions": views.sandbox_action_list(actions),
        }

    def target(self) -> dict | None:
        return self._target(self._desired())

    def revision(self) -> int | None:
        desired = self._desired()
        return None if desired is None else desired.generation

    def bindings(self) -> dict:
        desired, status, process = self._desired(), self._status(), self._supervisor.snapshot()
        members = desired.members if desired is not None else ()
        ours = {member.key for member in members if member.active}
        emulation = tuple(self._emulation_members())
        available = []
        for card in self._catalog.values():
            trades = (["emulation"] if emulation_traded(card, emulation) else []) + \
                     (["sandbox"] if card["id"] in ours else [])
            available.append({"id": card["id"], "instrument": card["instrument"], "strategy": card["strategy"],
                              "timeframe": card["timeframe"], "trades_in": trades})
        return {"revision": None if desired is None else desired.generation,
                "members": [self._member_view(member, status, process["running"]) for member in members],
                "available": available,
                "_links": views.links(self=views.SANDBOX_BINDINGS, sandbox=views.SANDBOX),
                "_actions": views.sandbox_action_list(("add-bindings",))}

    def reconcile(self) -> dict | None:
        return read_last_report(self.directory / BROKER_DATABASE_FILE)

    def log_tail(self, count: int) -> str:
        return tail_lines(self.directory / self._log_file, count)

    def summary(self) -> dict:
        desired, status, process = self._desired(), self._status(), self._supervisor.snapshot()
        report = self.reconcile()
        return {"state": self._state(desired, status, process),
                "bindings": sum(1 for member in (desired.members if desired else ()) if member.active),
                "frozen": len((status or {}).get("frozen") or []),
                "reconcile": None if report is None else report.get("result")}

    # --- команды ----------------------------------------------------------
    @staticmethod
    def _problem(status: HTTPStatus, code: str, message: str, **extra) -> tuple[int, dict]:
        return int(status), {"code": code, "message": message, **extra}

    def _rejected(self, ids) -> tuple[int, dict] | None:
        rejections = [{"binding": item, "reason": reason} for item, reason in unknown_bindings(ids, self._catalog)]
        if not rejections:
            return None
        self._rejections = rejections
        return self._problem(HTTPStatus.UNPROCESSABLE_ENTITY, "BINDINGS_REJECTED", "Состав не изменён.",
                             rejections=rejections)

    def put_target(self, mode: str, bindings: tuple[str, ...] | None, expected: int | None) -> tuple[int, dict]:
        with self._lock:
            if not self._supervisor.ready:
                return self._problem(HTTPStatus.CONFLICT, "CORE_NOT_READY", "Ядро стартует или повторяет старт.")
            try:
                return self._start(bindings, expected) if mode == views.MODE_ON else self._stop(mode, expected)
            except RevisionChanged as exc:
                return self._problem(HTTPStatus.PRECONDITION_FAILED, "REVISION_CHANGED", str(exc))

    def _start(self, bindings, expected) -> tuple[int, dict]:
        process = self._supervisor.snapshot()
        if process["running"]:
            desired = self._desired()
            if desired is not None and not desired.running:
                return self._problem(HTTPStatus.CONFLICT, "STOPPING", "Песочница останавливается.")
            return self._problem(HTTPStatus.CONFLICT, "ALREADY_RUNNING",
                                 "Песочница уже работает: состав меняется через /api/v1/sandbox/bindings.")
        busy = self._supervisor.busy_pid()
        if busy is not None:
            return self._problem(HTTPStatus.CONFLICT, "CONTOUR_DIR_BUSY",
                                 f"Папку контура держит процесс pid {busy}, который не подтверждает себя контуром.",
                                 pid=busy)
        current = self._desired()
        if bindings is None:
            members = [member for member in (current.members if current else ()) if member.active]
            ids = [member.key for member in members]
        else:
            rejected = self._rejected(bindings)
            if rejected is not None:
                return rejected
            ids, members = list(bindings), [binding_member(self._catalog[item]) for item in bindings]
        request = {"mode": views.MODE_ON, "bindings": ids}
        self._rejections = []
        self._core_state.record_launch(self.kind, self.kind, [], source=SOURCE_API,
                                       command=json.dumps(request, ensure_ascii=False), payload=request,
                                       members=members, expected=expected)
        self._supervisor.launch(self._desired())
        return int(HTTPStatus.ACCEPTED), self.view()

    def _stop(self, mode, expected) -> tuple[int, dict]:
        if not self._supervisor.snapshot()["running"]:
            return self._problem(HTTPStatus.CONFLICT, "NOT_RUNNING", "Процесса песочницы нет — останавливать нечего.")
        outcome = self._core_state.record_stop(self.kind, mode, expected)
        if outcome.outcome == CONFLICT:
            return self._problem(HTTPStatus.CONFLICT, "MODE_CONFLICT", outcome.reason)
        return (int(HTTPStatus.ACCEPTED) if outcome.outcome == ACCEPTED else int(HTTPStatus.OK)), self.view()

    def change_bindings(self, ids: tuple[str, ...], *, remove: bool, mode: str, expected: int | None) -> tuple[int, dict]:
        with self._lock:
            rejected = self._rejected(ids)
            if rejected is not None:
                return rejected
            if remove:
                desired = self._desired()
                present = {member.key for member in (desired.members if desired else ())}
                missing = [item for item in ids if item not in present]
                if missing:
                    rejections = [{"binding": item, "reason": "Привязки нет в составе песочницы."} for item in missing]
                    return self._problem(HTTPStatus.UNPROCESSABLE_ENTITY, "BINDINGS_REJECTED", "Состав не изменён.",
                                         rejections=rejections)
            try:
                outcome = self._core_state.record_bindings(
                    self.kind, self.kind, add=() if remove else [binding_member(self._catalog[item]) for item in ids],
                    remove=ids if remove else (), mode=mode, expected=expected)
            except RevisionChanged as exc:
                return self._problem(HTTPStatus.PRECONDITION_FAILED, "REVISION_CHANGED", str(exc))
            if outcome.outcome == CONFLICT:
                return self._problem(HTTPStatus.CONFLICT, "MODE_CONFLICT", outcome.reason)
            status = HTTPStatus.ACCEPTED if outcome.outcome == ACCEPTED else HTTPStatus.OK
            return int(status), self.bindings()
