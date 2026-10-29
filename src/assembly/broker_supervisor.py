"""Надзор ядра за контуром на брокере (add-sandbox-contour, design.md решения 2–3).

Свой поток, а не второй ``run_contour``: эмуляция и её главный цикл не меняются.
При старте ядра судьба контура решается тем же ``plan_recovery`` по его папке:
подхват, ожидание другой версии, автоподъём, ожидание команды. Запуск — по
команде API (``launch``); после выхода процесса ядро его само не перезапускает.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from src.assembly import recovery as contour_recovery
from src.assembly.contour_control import STATUS_FILE, read_status
from src.assembly.core_state import parse_time
from src.assembly.process import (STOP_TIMEOUT_SECS, AdoptedProcess, ContourProcess, contour_dir, terminate_pid,
                                  wait_exit, write_launch)
from src.logging_setup import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BrokerContourSupervisor:
    """Один контур на брокере: процесс, его выход и подъём после рестарта ядра."""

    def __init__(self, *, kind: str, state_dir: Path, core_state, version: str, autostart: bool,
                 heartbeat_timeout: float, heartbeat_secs: float, process_factory=ContourProcess,
                 adopt_factory=AdoptedProcess, say=print) -> None:
        self.kind = kind
        self.directory = contour_dir(state_dir, kind)
        self._core_state = core_state
        self._version = version
        self._autostart = autostart
        self._timeout = heartbeat_timeout
        self._heartbeat_secs = heartbeat_secs
        self._process_factory = process_factory
        self._adopt_factory = adopt_factory
        self._say = say
        self._lock = threading.RLock()
        self.process = None
        self.recovery: str | None = None
        self.started_at: str | None = None
        self.exit_code: int | None = None
        self.exited_at: str | None = None
        self.ready = False

    # --- подъём при старте ядра -------------------------------------------
    def recover(self) -> str:
        """Судьба контура, когда ядро готово; вызывается раньше подъёма эмуляции."""
        with self._lock:
            self.ready = True
            plan = self._plan()
            if plan.action == contour_recovery.REPLACE:
                self._say(f"Контур {self.kind} (pid {plan.pid}) другой версии или уже выходит — ядро ждёт его выхода")
                if not wait_exit(plan.pid, STOP_TIMEOUT_SECS + self._heartbeat_secs):
                    log.error("Контур %s pid %s не вышел — завершается принудительно", self.kind, plan.pid)
                    terminate_pid(plan.pid)
                    wait_exit(plan.pid, 5)
                plan = self._plan()
                if plan.action in (contour_recovery.ADOPT, contour_recovery.REPLACE):
                    plan = contour_recovery.RecoveryPlan(contour_recovery.BLOCKED, plan.pid, plan.status)
            desired = self._core_state.desired(self.kind)
            if plan.action == contour_recovery.BLOCKED:
                message = (f"Папка контура {self.directory} занята процессом pid {plan.pid}, который не подтверждает "
                           f"себя как контур робота. Контур {self.kind} не запускается; эмуляция и API работают.")
                print(message)
                log.error("%s", message)
            elif plan.action == contour_recovery.ADOPT:
                self._adopt(plan.pid)
                self._say(f"Контур {self.kind} подхвачен: pid {plan.pid}, поколение {desired.generation}")
            elif plan.action == contour_recovery.AUTOSTART or self._should_autostart(desired, plan):
                self._start(desired, recovery=contour_recovery.RECOVERY_AUTOSTART)
                self._say(f"Контур {self.kind} поднимается без команды: поколение {desired.generation}")
            return plan.action

    def _plan(self):
        return contour_recovery.plan_recovery(self.directory, version=self._version,
                                              desired=self._core_state.desired(self.kind), explicit=False,
                                              autostart=self._autostart, timeout=self._timeout)

    def _should_autostart(self, desired, plan) -> bool:
        # plan_recovery требует непустой состав; контуру на брокере пустой состав не мешает.
        return (plan.action == contour_recovery.WAIT and self._autostart
                and contour_recovery.should_run(desired, plan.status))

    def busy_pid(self) -> int | None:
        """pid чужого процесса в папке контура; иначе ``None``."""
        plan = contour_recovery.plan_recovery(self.directory, version=self._version, desired=None, explicit=True,
                                              autostart=False, timeout=self._timeout)
        return plan.pid if plan.action == contour_recovery.BLOCKED else None

    # --- запуск и выход ---------------------------------------------------
    @property
    def running(self) -> bool:
        return self.process is not None and self.process.running

    def launch(self, desired) -> int:
        """Запуск по команде: желаемое уже записано в ``core.sqlite3``."""
        with self._lock:
            return self._start(desired, recovery=None)

    def _start(self, desired, *, recovery: str | None) -> int:
        import os

        write_launch(self.directory, {
            "contour_id": self.kind, "kind": self.kind, "core_pid": os.getpid(), "core_version": self._version,
            "generation": desired.generation,
            "bindings": [member.key for member in desired.members if member.active],
        })
        self.recovery, self.exit_code, self.exited_at = recovery, None, None
        self.started_at = _now().isoformat().replace("+00:00", "Z")
        self.process = self._process_factory(self.kind, on_exit=self._exited)
        pid = self.process.start()
        log.info("Контур %s запущен: pid %s, поколение %s", self.kind, pid, desired.generation)
        return pid

    def _adopt(self, pid: int) -> None:
        adopted_at = _now()
        status_path = self.directory / STATUS_FILE

        def exited(code, stopping):
            status = read_status(status_path) or {}
            try:
                clean = bool(status.get("exited_at")) and parse_time(status["exited_at"]) >= adopted_at
            except (TypeError, ValueError):
                clean = False
            self._exited(0 if clean else None, stopping)

        self.recovery = contour_recovery.RECOVERY_ADOPTED
        self.started_at = adopted_at.isoformat().replace("+00:00", "Z")
        self.process = self._adopt_factory(self.kind, pid, on_exit=exited)
        self.process.start()

    def _exited(self, code, stopping) -> None:
        self.exit_code = code
        self.exited_at = _now().isoformat().replace("+00:00", "Z")
        if code not in (0, None) or code is None and not stopping:
            log.error("Контур %s завершился с кодом %s; ядро его само не перезапускает", self.kind, code)

    def stop(self) -> None:
        """Остановка робота: ``stop`` процессу и ожидание выхода."""
        with self._lock:
            if self.process is not None:
                self.process.stop()

    def snapshot(self) -> dict:
        process = self.process
        return {"pid": None if process is None else process.pid, "exit_code": self.exit_code,
                "exited_at": self.exited_at, "recovery": self.recovery, "started_at": self.started_at,
                "running": self.running}
