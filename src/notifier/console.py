"""Консольный канал: печатает уведомление в stdout.

Консоль — источник истины для пользователя: если канал доставки не настроен
или молчит, это видно по отсутствию строки здесь. Никаких проверок на «а
не дублируется ли в консоли» в коде быть не должно.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import IO, Callable, Iterable

from src.events.event import Event
from src.events.types import CONTROL_EVENT_TYPES, NOTIFICATION_EVENT_TYPES, EventType
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.templates import render
from src.notifier.templates.decision import idle_tick_summary

log = get_logger(__name__)


class ConsoleChannel(Channel):
    """Печатает текст уведомления в поток вывода."""

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
        text = render(event, self._tz_offset_hours)
        if text is None:
            return
        printed = self._write(text)
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
                self._write(idle_tick_summary(moscow, self._hold_count, self._hold_instruments))
        finally:
            self._active_tick_id = None
            self._hold_count = 0
            self._hold_instruments.clear()
            self._printed_in_tick = False

    def _write(self, text: str) -> bool:
        try:
            if self._stream is None:
                print(text, flush=True)
            else:
                print(text, file=self._stream, flush=True)
            return True
        except OSError:
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
