"""Консольный канал: печатает уведомление в stdout.

Консоль — источник истины для пользователя: если канал доставки не настроен
или молчит, это видно по отсутствию строки здесь. Никаких проверок на «а
не дублируется ли в консоли» в коде быть не должно.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import shutil
import sys
from threading import Lock
from typing import IO, Callable, Iterable

from src.events.event import Event
from src.events.types import CONTROL_EVENT_TYPES, NOTIFICATION_EVENT_TYPES, EventType
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.templates import render
from src.notifier.templates.decision import idle_tick_summary

log = get_logger(__name__)


def _supports_color(stream: IO[str], *, platform_name: str | None = None) -> bool:
    if "NO_COLOR" in os.environ or os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
    except (OSError, ValueError):
        is_tty = False
    if not is_tty:
        return False
    if (platform_name or os.name) != "nt":
        return True
    # В старой консоли Windows ANSI допустим лишь после включения VT-режима.
    if stream not in (sys.stdout, sys.stderr):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = wintypes.DWORD()
        if not handle or handle == ctypes.c_void_p(-1).value or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004) or bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


class ConsoleChannel(Channel):
    """Печатает один завершённый блок уведомления в поток вывода."""

    name = "console"
    notification_types: frozenset[EventType] = NOTIFICATION_EVENT_TYPES
    control_types: frozenset[EventType] = CONTROL_EVENT_TYPES

    def __init__(
        self,
        *,
        tz_offset_hours: float = 0.0,
        supported_types: Iterable[EventType] | None = None,
        stream: IO[str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(supported_types=supported_types)
        self._tz_offset_hours = tz_offset_hours
        self._stream = stream
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._write_lock = Lock()
        self._disabled = False
        self._active_tick_id: str | None = None
        self._hold_count = 0
        self._hold_instruments: dict[str, None] = {}
        self._printed_in_tick = False

    def handle(self, event: Event) -> None:
        if self._disabled:
            return
        if event.type is EventType.TICK_STARTED:
            self._begin_tick(str(event.get("tick_id") or ""))
            return
        if event.type is EventType.TICK_FINISHED:
            self._finish_tick(str(event.get("tick_id") or ""), bool(event.get("completed")))
            return
        if event.type not in self.notification_types:
            return
        if self._is_clean_hold(event):
            self._remember_hold(event)
            return
        stream = self._stream if self._stream is not None else sys.stdout
        text = render(
            event, self._tz_offset_hours, now=self._now(),
            width=shutil.get_terminal_size(fallback=(96, 24)).columns,
            color=_supports_color(stream),
        )
        if text is None:
            return
        printed = self._write(text, separate_after=event.type is not EventType.HEARTBEAT)
        if printed and self._active_tick_id is not None:
            self._printed_in_tick = True

    @staticmethod
    def _is_clean_hold(event: Event) -> bool:
        return (
            event.type is EventType.DECISION
            and event.get("outcome") == "no_signal"
            and not event.get("filtered_out")
        )

    def _begin_tick(self, tick_id: str) -> None:
        self._active_tick_id = tick_id
        self._hold_count = 0
        self._hold_instruments.clear()
        self._printed_in_tick = False

    def _remember_hold(self, event: Event) -> None:
        if self._active_tick_id is None:
            return
        self._hold_count += 1
        self._hold_instruments.setdefault(event.instrument or "контракт не указан", None)

    def _finish_tick(self, tick_id: str, completed: bool) -> None:
        if tick_id != self._active_tick_id:
            return
        try:
            if completed and self._hold_count and not self._printed_in_tick:
                moment = self._now()
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                moscow = moment.astimezone(timezone(timedelta(hours=3)))
                stream = self._stream if self._stream is not None else sys.stdout
                self._write(idle_tick_summary(
                    moscow, self._hold_count, self._hold_instruments,
                    color=_supports_color(stream),
                ))
        finally:
            self._active_tick_id = None
            self._hold_count = 0
            self._hold_instruments.clear()
            self._printed_in_tick = False

    def _write(self, text: str, *, separate_after: bool = False) -> bool:
        try:
            with self._write_lock:
                stream = self._stream if self._stream is not None else sys.stdout
                stream.write(text + ("\n\n" if separate_after else "\n"))
                stream.flush()
            return True
        except (OSError, ValueError):
            # Закрытый stdout (запуск из-под мёртвого терминала, `head` и т.п.):
            # печать отключается, чтобы не заливать лог повторными ошибками.
            self._disabled = True
            log.warning("Консоль недоступна (вывод закрыт): печать уведомлений отключена.")
            return False

    def close(self) -> None:
        self._active_tick_id = None
        self._hold_count = 0
        self._hold_instruments.clear()
        self._printed_in_tick = False
