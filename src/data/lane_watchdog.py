"""Сторож полос данных: шаг, который не вернулся за порог (fix-live-lane-stall).

Цикл полосы обрамляет ``step()`` вызовами ``begin``/``end``; пауза между
шагами шагом не считается. Сторож только наблюдает: поток полосы не
прерывается и не заменяется, ядро не перезапускается (design 1, Non-Goals).
Стек — файлы, строки и исходный код кадров, без локальных переменных.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from src.logging_setup import get_logger

log = get_logger(__name__)

STATE_OK = "ok"
STATE_STALLED = "stalled"


def _wall() -> datetime:
    return datetime.now(timezone.utc)


def _utc(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%d.%m %H:%M:%S")


class LaneWatchdog:
    """Учёт текущих шагов полос и тревога о зависшем шаге."""

    def __init__(self, threshold: float, *, clock=time.monotonic, wall=_wall, announce=print,
                 frames=sys._current_frames) -> None:
        self.threshold = float(threshold)
        self._clock = clock
        self._wall = wall
        self._announce = announce
        self._frames = frames
        self._lock = threading.Lock()
        self._steps: dict[str, dict] = {}

    def register(self, name: str) -> None:
        """Полоса известна снимку ещё до первого шага."""
        with self._lock:
            self._entry(name)

    def _entry(self, name):
        return self._steps.setdefault(name, {"started": None, "started_at": None, "ident": None,
                                             "last_step_at": None, "alerted": False})

    def begin(self, name: str) -> None:
        with self._lock:
            self._entry(name).update(started=self._clock(), started_at=self._wall(),
                                     ident=threading.get_ident(), alerted=False)

    def end(self, name: str) -> None:
        with self._lock:
            entry = self._entry(name)
            started, alerted = entry["started"], entry["alerted"]
            entry.update(started=None, started_at=None, ident=None, alerted=False, last_step_at=self._wall())
        if alerted and started is not None:
            log.info("Полоса данных %s снова работает: шаг длился %d с", name, self._clock() - started)

    def check(self) -> list[str]:
        """Новые зависшие шаги: один WARNING со стеком и строка в консоль на шаг."""
        now = self._clock()
        found = []
        with self._lock:
            for name, entry in self._steps.items():
                if entry["started"] is not None and not entry["alerted"] and now - entry["started"] >= self.threshold:
                    entry["alerted"] = True
                    found.append((name, entry["started_at"], now - entry["started"], entry["ident"]))
        for name, started_at, elapsed, ident in found:
            log.warning("Полоса данных %s не отвечает: шаг идёт с %s (%d с). Стек потока полосы:\n%s",
                        name, _local(started_at), elapsed, self._stack(ident))
            self._announce(f"Полоса данных {name} не отвечает с {_local(started_at)}: данные по ней не обновляются, "
                           f"нужен перезапуск робота. Стек — в bot_debug.log")
        return [name for name, *_ in found]

    def _stack(self, ident) -> str:
        frame = self._frames().get(ident) if ident is not None else None
        if frame is None:
            return "  (кадр потока недоступен)"
        return "".join(traceback.format_stack(frame)).rstrip()

    def stalled(self) -> list[tuple[str, datetime]]:
        """Зависшие сейчас полосы: (имя, начало шага) — для сводки полос."""
        now = self._clock()
        with self._lock:
            return [(name, entry["started_at"]) for name, entry in sorted(self._steps.items())
                    if entry["started"] is not None and now - entry["started"] >= self.threshold]

    def stalled_text(self) -> str:
        """Хвост сводки полос: «; зависли: live с 01.11 07:50:02» или пусто."""
        stalled = self.stalled()
        if not stalled:
            return ""
        return "; зависли: " + ", ".join(f"{name} с {_local(started_at)}" for name, started_at in stalled)

    def snapshot(self) -> dict[str, dict]:
        """Состояние полос для ``GET /api/v1/core``: только время, без стеков."""
        now = self._clock()
        with self._lock:
            return {name: {"state": STATE_STALLED if entry["started"] is not None and now - entry["started"] >= self.threshold
                           else STATE_OK,
                           "step_started_at": _utc(entry["started_at"]),
                           "last_step_at": _utc(entry["last_step_at"])}
                    for name, entry in sorted(self._steps.items())}
