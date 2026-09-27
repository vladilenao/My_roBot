"""Шаблоны канала: превращают событие в текст.

Шаблон принадлежит каналу, а не шине и не общему слою: канал сам решает,
что показывать пользователю. Общие примитивы допустимы только в
``common.py`` — это приведение цены и сборка заголовочной части.
"""

from __future__ import annotations

from src.events.event import Event
from src.notifier.templates import decision, deal, signal, system

_RENDERERS = (decision.render, signal.render, system.render, deal.render)


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    """Текст уведомления или ``None``, если у события нет представления."""
    for renderer in _RENDERERS:
        text = renderer(event, tz_offset_hours)
        if text is not None:
            return text
    return None


__all__ = ["render"]
