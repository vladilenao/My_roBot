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
from src.notifier.templates.console_view import render_status_group, status_label
from src.notifier.templates.decision import idle_tick_summary
from src.notifier.templates.holds import hold_key

log = get_logger(__name__)


def _cause(event: Event) -> tuple:
    """Причина состояния для 15-минутного повтора: сдвиг удержания — смена причины."""
    return event.get("reason") or "", hold_key(event.get("hold"))


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
        supported_types: Iterable[EventType] | None = None,
        stream: IO[str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(supported_types=supported_types)
        self._stream = stream
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._write_lock = Lock()
        self._disabled = False
        self._active_tick_id: str | None = None
        self._hold_pairs: set[tuple[str, str]] = set()
        self._printed_in_tick = False
        self._api_limit_active = False
        self._api_limit_anchor: tuple[str, str] | None = None
        self._last_live_notice: dict[tuple[str, str], tuple[str, str, datetime]] = {}
        self._pending_statuses: dict[tuple[str, str, str], Event] = {}
        self._has_output = False
        self._last_type: EventType | None = None
        self._last_trade_id = ""
        self._last_decision_id = ""

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
        if event.type is EventType.STARTUP_ANALYSIS:
            return
        if event.type is EventType.MARKET_DATA_STATUS:
            self._handle_status(event)
            return
        if event.type is EventType.HEARTBEAT:
            self._flush_statuses()
        if self._is_clean_hold(event):
            self._remember_hold(event)
            return
        linked = (event.type in {EventType.SIGNAL, EventType.REJECTED}
                  and self._last_type is EventType.DECISION
                  and bool(self._last_decision_id)
                  and event.get("event_id") == self._last_decision_id)
        if event.type is EventType.ORDER_REJECTED:
            # Вторичный ОРДЕР продолжает блок своей сделки; иначе он самостоятельный.
            linked = (self._last_type in {EventType.TRADE_CLOSED, EventType.TARGET_HIT}
                      and bool(event.get("trade_id")) and event.get("trade_id") == self._last_trade_id)
        stream = self._stream if self._stream is not None else sys.stdout
        rendered = render(
            event, now=self._now(),
            width=shutil.get_terminal_size(fallback=(96, 24)).columns,
            color=_supports_color(stream), linked=linked,
        )
        if rendered is None:
            return
        join = linked or (event.type is EventType.DECISION and self._last_type is EventType.DECISION)
        printed = self._write(rendered, join=join)
        if printed and self._active_tick_id is not None:
            self._printed_in_tick = True
        if printed:
            self._last_type = event.type
            self._last_trade_id = event.get("trade_id") or ""
            self._last_decision_id = (event.get("event_id") or "") if event.type is EventType.DECISION else ""

    def _handle_status(self, event: Event) -> None:
        state = event.get("data_state")
        key = (event.instrument, event.timeframe)
        if state == "rate_limited":
            if self._api_limit_active:
                return
            self._api_limit_active = True
            self._api_limit_anchor = key
        elif key == self._api_limit_anchor:
            self._api_limit_active = False
            self._api_limit_anchor = None
        if state in {"current", "waiting_bar"} and not event.get("reason") and not event.get("hold"):
            previous = self._last_live_notice.pop(key, None)
            if previous is None or previous[0] in {"current", "waiting_bar", "recovered"}:
                return
            event = Event.market_data_status(event.instrument, timeframe=event.timeframe,
                session=event.get("session") or "unknown", data_state="recovered",
                observed_at=event.get("observed_at"), reason="",
                received_bar=event.get("received_bar"), analyzed_bar=event.get("analyzed_bar"))
            state = "recovered"
        elif state != "rate_limited":
            previous = self._last_live_notice.get(key)
            if previous and previous[:2] == (state, _cause(event)) and self._now() - previous[2] < timedelta(minutes=15):
                return
        if status_label(event) is None:
            return
        if self._active_tick_id is not None:
            self._pending_statuses[(status_label(event) or "", *key)] = event
        else:
            self._print_statuses((event,))

    def _print_statuses(self, events: Iterable[Event]) -> None:
        groups: dict[str, list[Event]] = {}
        for event in events:
            label = status_label(event)
            if label is not None:
                groups.setdefault(label, []).append(event)
        stream = self._stream if self._stream is not None else sys.stdout
        for label, items in groups.items():
            rendered = render_status_group(items,
                width=shutil.get_terminal_size(fallback=(96, 24)).columns,
                color=_supports_color(stream))
            if rendered is None or not self._write(rendered):
                continue
            if self._active_tick_id is not None:
                self._printed_in_tick = True
            self._last_type = EventType.MARKET_DATA_STATUS
            self._last_decision_id = ""
            for event in items:
                if event.get("data_state") != "rate_limited":
                    self._last_live_notice[(event.instrument, event.timeframe)] = (
                        event.get("data_state"), _cause(event), self._now())

    def _flush_statuses(self) -> None:
        if self._pending_statuses:
            pending = tuple(self._pending_statuses.values())
            self._pending_statuses.clear()
            self._print_statuses(pending)

    @staticmethod
    def _is_clean_hold(event: Event) -> bool:
        return (
            event.type is EventType.DECISION
            and event.get("outcome") == "no_signal"
            and not event.get("filtered_out")
            and event.get("analysis_mode") != "historical"
        )

    def _begin_tick(self, tick_id: str) -> None:
        self._flush_statuses()
        self._active_tick_id = tick_id
        self._hold_pairs.clear()
        self._printed_in_tick = False

    def _remember_hold(self, event: Event) -> None:
        if self._active_tick_id is None:
            return
        self._hold_pairs.add((event.instrument or "контракт не указан", event.timeframe))

    def _finish_tick(self, tick_id: str, completed: bool) -> None:
        if tick_id != self._active_tick_id:
            return
        try:
            self._flush_statuses()
            if completed and self._hold_pairs and not self._printed_in_tick:
                moment = self._now()
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                moscow = moment.astimezone(timezone(timedelta(hours=3)))
                stream = self._stream if self._stream is not None else sys.stdout
                self._write(idle_tick_summary(
                    moscow, len(self._hold_pairs),
                    color=_supports_color(stream),
                ))
        finally:
            self._active_tick_id = None
            self._hold_pairs.clear()
            self._printed_in_tick = False

    def _write(self, text: str, *, join: bool = False) -> bool:
        try:
            with self._write_lock:
                stream = self._stream if self._stream is not None else sys.stdout
                stream.write(("\n" if self._has_output and not join else "") + text + "\n")
                stream.flush()
                self._has_output = True
            return True
        except (OSError, ValueError):
            # Закрытый stdout (запуск из-под мёртвого терминала, `head` и т.п.):
            # печать отключается, чтобы не заливать лог повторными ошибками.
            self._disabled = True
            log.warning("Консоль недоступна (вывод закрыт): печать уведомлений отключена.")
            return False

    def close(self) -> None:
        self._flush_statuses()
        self._active_tick_id = None
        self._hold_pairs.clear()
        self._printed_in_tick = False
