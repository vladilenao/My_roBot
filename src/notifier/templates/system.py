"""Шаблоны служебных событий: сердцебиение и сбой."""

from __future__ import annotations

from src.events.event import Event
from src.events.types import EventType


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    if event.type is EventType.HEARTBEAT:
        return (
            f"💓 Сердцебиение: тиков работы — {event.get('tick_count', 0)}, "
            f"ошибок за период — {event.get('error_count', 0)}."
        )
    if event.type is EventType.ERROR:
        return (
            f"❗ Сбой: {event.get('operation') or 'выполнение операции'}. "
            f"Робот продолжает работу. "
            f"Подробности — в bot_debug.log рядом с роботом."
        )
    return None
