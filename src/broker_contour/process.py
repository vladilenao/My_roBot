"""Цикл процесса контура на брокере (add-sandbox-contour, design.md решение 4).

Допуск до первого шага, затем шаг раз в ``step_secs``: целевое состояние из
``core.sqlite3``, сверка по расписанию, ``status.json``. Пульс ядра идёт своим
потоком (``CorePulse``) и через ``touch`` освежает ``alive_at``. На этапе С2
контур только читает счёт: остановка любого режима — выход на шаге её применения.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.assembly.contour_control import STATUS_FILE, write_status
from src.assembly.core_state import read_desired
from src.broker_contour.account import AccountSnapshot, read_account
from src.broker_contour.admission import AdmissionError, admit_account, check_settings
from src.broker_contour.notify import BrokerNotifier
from src.broker_contour.reconcile import JournalView, Reconciler
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.logging_setup import get_logger, register_secrets
from src.sandbox.gateway import BrokerCallError

log = get_logger(__name__)

STEP_SECS = 10
STOPPED, FAILED, RUNNING, STARTING = "stopped", "failed", "running", "starting"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BrokerContourSettings:
    contour_id: str
    broker: str
    directory: Path
    core_db: Path
    version: str
    token: str | None
    token_env: str
    bot_token: str | None
    bot_env: str
    channel_id: str | None
    channel_env: str
    reconcile_every_secs: float
    step_secs: float = STEP_SECS


class BrokerContour:
    """Процесс контура на брокере. ``gateway_factory(token)`` — контекстный менеджер шлюза брокера."""

    def __init__(self, settings: BrokerContourSettings, *, gateway_factory: Callable, bus, stop,
                 pid: int, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.settings = settings
        settings.directory.mkdir(parents=True, exist_ok=True)
        self._gateway_factory = gateway_factory
        self._stop = stop
        self._pid = pid
        self._clock = clock
        self._lock = threading.Lock()
        self.notifier = BrokerNotifier(bus, contour=settings.contour_id)
        self._status = {"contour_id": settings.contour_id, "kind": settings.contour_id, "state": STARTING,
                        "applied_generation": None, "stop_mode": None, "version": settings.version, "pid": pid,
                        "members": [], "account": None, "capital": None, "positions": [], "orders": 0,
                        "stop_orders": 0, "frozen": [], "last_reconcile_at": None, "account_link": None,
                        "core_link": "ok", "reason": None, "exited_at": None, "exit_reason": None}
        self._generation: int | None = None

    # --- status.json ------------------------------------------------------
    def _write(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)
            payload = {**self._status, "alive_at": _now(), "updated_at": _now()}
            try:
                write_status(self.settings.directory / STATUS_FILE, payload)
            except OSError as exc:
                log.warning("status.json контура не записан (%s)", type(exc).__name__)

    def touch(self, link: str) -> None:
        """Пульс ядра (поток ``CorePulse``): связь и свежий ``alive_at`` без ожидания шага."""
        self._write(core_link=link)

    # --- жизнь процесса ---------------------------------------------------
    def run(self) -> int:
        settings = self.settings
        try:
            check_settings(token=settings.token, token_env=settings.token_env, bot_token=settings.bot_token,
                           bot_env=settings.bot_env, channel_id=settings.channel_id, channel_env=settings.channel_env)
        except AdmissionError as exc:
            return self._refuse(exc)
        store = BrokerStore(settings.directory / BROKER_DATABASE_FILE)
        try:
            with self._gateway_factory(settings.token) as gateway:
                try:
                    admitted = admit_account(gateway, store, broker=settings.broker)
                except AdmissionError as exc:
                    return self._refuse(exc)
                return self._serve(gateway, store, admitted)
        except BrokerCallError as exc:
            return self._refuse(AdmissionError(f"Брокер недоступен: {exc}", alarm=True))

    def _refuse(self, exc: AdmissionError) -> int:
        log.error("Контур %s не стартует: %s", self.settings.contour_id, exc.reason)
        print(f"Контур {self.settings.contour_id} не стартует: {exc.reason}")
        if exc.alarm:
            self.notifier.account_tail = exc.account_tail
            self.notifier.alarm([], (), reason=f"Контур не стартует: {exc.reason}")
        self._write(state=FAILED, reason=exc.reason, exited_at=_now(), exit_reason=exc.reason)
        return 2

    def _serve(self, gateway, store: BrokerStore, admitted) -> int:
        settings = self.settings
        account = admitted.account
        register_secrets([account.account_id])
        self.notifier.account_tail = account.masked
        reconciler = Reconciler(store=store, notifier=self.notifier, account_tail=account.masked,
                                every_secs=settings.reconcile_every_secs)
        desired = read_desired(settings.core_db, settings.contour_id)
        self._apply(desired)
        self._account(admitted.snapshot)
        self.notifier.started(admitted.snapshot, bindings=len(self._status["members"]), generation=self._generation)
        log.info("Контур %s запущен: счёт %s, поколение %s", settings.contour_id, account.masked, self._generation)
        self._reconcile(reconciler, gateway, account, admitted.snapshot)
        self._write(state=RUNNING, account=account.masked, account_link="ok")
        while not self._stop.requested.wait(settings.step_secs):
            self.step(gateway, account, reconciler)
        reason = self._stop.reason or "цикл завершён"
        self.notifier.stopped(reason)
        self._write(state=STOPPED, exited_at=_now(), exit_reason=reason)
        log.info("Контур %s остановлен: %s", settings.contour_id, reason)
        return 0

    def step(self, gateway, account, reconciler: Reconciler) -> None:
        """Один шаг: целевое состояние, сверка по расписанию, ``status.json``."""
        self._apply(read_desired(self.settings.core_db, self.settings.contour_id))
        if self._stop.requested.is_set():
            return
        if reconciler.due(self._clock()):
            self._reconcile(reconciler, gateway, account)
        self._write()

    def _apply(self, desired) -> None:
        if desired is None:
            return
        if desired.generation != self._generation:
            self._generation = desired.generation
            members = [{"key": member.key, "name": member.key, "state": "watching" if member.active else "removed"}
                       for member in desired.members]
            self._write(applied_generation=desired.generation, members=members, stop_mode=desired.stop_mode)
            log.info("Контур %s: применено поколение %s", self.settings.contour_id, desired.generation)
        if not desired.running and desired.stop_mode:
            # С2: своих сделок нет — остановка любого режима завершает процесс; счёт не трогается.
            self._stop.request(f"остановка командой ({desired.stop_mode})")

    def _account(self, snapshot: AccountSnapshot) -> None:
        self._write(**snapshot.payload(), account_link="ok")

    def _reconcile(self, reconciler: Reconciler, gateway, account, snapshot: AccountSnapshot | None = None) -> None:
        now = self._clock()
        if snapshot is None:
            try:
                snapshot = read_account(gateway, account)
            except BrokerCallError as exc:
                log.warning("Сверка пропущена: счёт не прочитан (%s)", exc.code)
                report = reconciler.run(now, None)
                self._write(account_link="lost", last_reconcile_at=report["at"])
                return
            self._account(snapshot)
        report = reconciler.run(now, snapshot, JournalView())
        self._write(frozen=report["frozen"], last_reconcile_at=report["at"])
