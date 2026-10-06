"""Консольный канал: печатает уведомление в stdout.

Консоль — источник истины для пользователя: если канал доставки не настроен
или молчит, это видно по отсутствию строки здесь. Никаких проверок на «а
не дублируется ли в консоли» в коде быть не должно.
"""

from __future__ import annotations

from typing import IO, Iterable

from src.events.event import Event
from src.events.types import ALL_EVENT_TYPES, EventType
from src.notifier.channel import Channel
from src.notifier.templates import render


class ConsoleChannel(Channel):
    """Печатает текст уведомления в поток вывода."""

    name = "console"
    supported_types: frozenset[EventType] = ALL_EVENT_TYPES

    def __init__(
        self,
        *,
        tz_offset_hours: float = 0.0,
        supported_types: Iterable[EventType] | None = None,
        stream: IO[str] | None = None,
    ) -> None:
        super().__init__(supported_types=supported_types)
        self._tz_offset_hours = tz_offset_hours
        self._stream = stream

    def handle(self, event: Event) -> None:
        if not self.accepts(event):
            return
        text = render(event, self._tz_offset_hours)
        if text is None:
            return
        if self._stream is None:
            print(text, flush=True)
        else:
            print(text, file=self._stream, flush=True)
