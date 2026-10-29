"""Пульс ядра и связь контура с ним (add-contour-recovery).

Ядро раз в ``heartbeat_secs`` пишет удар в ``core.sqlite3`` (``CorePulseWriter``),
при штатной остановке — ``stopped``. Процесс контура тем же шагом читает
пульс только на чтение (``CorePulse``):

- удара нет дольше ``heartbeat_timeout_secs`` — связь ``lost``, контур
  перестаёт входить в новые сделки и ждёт ядро без срока;
- ``stopped`` или свежий удар ядра другой версии — остановка после тика.

Каждую проверку контур отмечает в ``status.json`` (``alive_at``): так новое
ядро отличает живой контур от процесса с тем же pid. Модуль не импортирует
ни ядро, ни контур.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.assembly.core_state import read_heartbeat
from src.logging_setup import get_logger

log = get_logger(__name__)

LINK_OK, LINK_LOST = "ok", "lost"
STOP_CORE_STOPPED = "ядро остановлено"
STOP_CORE_VERSION = "ядро другой версии"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CorePulseWriter:
    """Сторона ядра: первый удар в ``start``, затем поток; ``stop`` пишет ``stopped``."""

    def __init__(self, store, *, pid: int, version: str, interval: float) -> None:
        self._store = store
        self._pid = pid
        self._version = version
        self._interval = interval
        self.started_at = _now()
        self.last = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed = False
        self._stopped = False

    def start(self) -> None:
        self._beat()
        self._thread = threading.Thread(target=self._run, name="core-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._halt.wait(self._interval):
            self._beat()

    def _beat(self) -> None:
        try:
            self.last = self._store.beat(self._pid, self._version, self.started_at)
        except sqlite3.Error as exc:
            if not self._failed:
                log.warning("Пульс ядра не записан (%s): контуры без пульса перестанут входить", type(exc).__name__)
            self._failed = True
            return
        if self._failed:
            log.info("Пульс ядра снова пишется")
        self._failed = False

    def stop(self) -> None:
        """Штатная остановка ядра: поток пульса завершается, строка получает ``stopped``. Повтор безопасен."""
        if self._stopped:
            return
        self._stopped = True
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
        try:
            self._store.mark_stopped(self._pid)
        except sqlite3.Error as exc:
            log.warning("Остановка ядра не записана в пульс (%s)", type(exc).__name__)

    def payload(self) -> dict:
        last = self.last
        return {"beat_at": None if last is None else last.beat_at, "interval_secs": self._interval}


class CorePulse:
    """Сторона контура: связь с ядром по пульсу, остановка по ``stopped`` и чужой версии."""

    def __init__(self, core_db: Path, *, version: str, interval: float, timeout: float,
                 on_stop: Callable[[str], None], on_check: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._core_db = Path(core_db)
        self._version = version
        self._interval = interval
        self._timeout = timeout
        self._on_stop = on_stop
        self._on_check = on_check
        self._clock = clock
        # Ядро только что запустило или подхватывает контур: отсчёт — от старта процесса.
        self._seen = clock()
        self._last_beat: str | None = None
        self._link = LINK_OK
        self._stopping = False
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def link(self) -> str:
        return self._link

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="contour-core-pulse", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        while not self._halt.wait(self._interval):
            try:
                self.check()
            except Exception:  # поток пульса не должен умирать молча: контур остался бы без признака жизни
                log.exception("Проверка пульса ядра не выполнена")

    def check(self) -> str:
        """Одна проверка: обновляет связь, при ``stopped`` и чужой версии просит остановку."""
        try:
            beat = read_heartbeat(self._core_db)
        except sqlite3.Error:
            beat = None
        if beat is not None:
            fresh = beat.age() <= self._timeout
            if beat.stopped:
                self._stop(STOP_CORE_STOPPED)
            elif fresh and beat.core_version != self._version:
                self._stop(STOP_CORE_VERSION)
            elif fresh:
                self._seen = self._clock()
                self._last_beat = beat.beat_at
                if self._link != LINK_OK:
                    log.info("Связь с ядром восстановлена: ядро pid %s, запрет входов снимается", beat.core_pid)
                self._link = LINK_OK
        if self._link == LINK_OK and self._clock() - self._seen > self._timeout:
            self._link = LINK_LOST
            log.warning("Связь с ядром потеряна: последний удар %s, новых входов нет до возврата ядра",
                        self._last_beat or "не получен")
        if self._on_check is not None:
            self._on_check(self._link)
        return self._link

    def _stop(self, reason: str) -> None:
        if not self._stopping:
            self._stopping = True
            log.info("Контур останавливается: %s", reason)
            self._on_stop(reason)
