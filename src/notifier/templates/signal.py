"""Шаблон рекомендации: сделка допущена, заявка ждёт подтверждения."""

from __future__ import annotations

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates.common import price


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    if event.type is not EventType.SIGNAL:
        return None
    label = f"● {event.instrument}"
    if event.timeframe:
        label += f" ({event.timeframe})"
    targets = ", ".join(price(target) for target in event.get("targets", ())) or "нет"
    text = (
        f"{label} ➜ Сделка {event.get('side')}, объём {event.get('quantity', 0)} — "
        f"Вход: {price(event.get('entry'))}, "
        f"Стоп: {price(event.get('stop'))}, Цели: {targets} "
        f"— в работе, ждёт подтверждения"
    )
    expected_r = event.get("expected_r")
    if expected_r is not None:
        text += f", ожидаемый результат {price(expected_r)}R"
    return text
