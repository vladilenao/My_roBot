"""Общие примитивы шаблонов: цена и заголовочная часть уведомления."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from src.events.event import Event


def price(value: Any) -> str:
    """Цена без хвостовых нулей."""
    if value is None:
        return ""
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def amount(value: Any) -> str:
    """Число в том виде, в каком его писал исполнитель.

    Отличие от ``price`` намеренное: цена в уведомлении о сделке печатается
    ровно так, как её посчитал брокер, поэтому ``100.0`` не превращается в
    ``100`` и запись события не меняет вид.
    """
    if value is None:
        return ""
    return format(value, "f")


def pnl_text(value: Any) -> str:
    """Финансовый результат компактной записью, как в журнале."""
    if value is None:
        return ""
    return format(float(value), "g")


def header_parts(event: Event, marker: str, tz_offset_hours: float) -> list[str]:
    """Заголовочные части: маркер с инструментом, время бара, стратегия с профилем."""
    parts: list[str] = []
    if event.instrument:
        label_block = f"{marker} {event.instrument}"
        if event.timeframe:
            label_block += f" ({event.timeframe})"
        parts.append(label_block)
    else:
        parts.append(marker)
    if event.bar_time is not None:
        parts.append(
            (event.bar_time + timedelta(hours=tz_offset_hours)).strftime("%H:%M")
        )
    strategy = event.get("strategy") or ""
    if strategy:
        strategy_block = f"| {strategy}"
        filter_profile = event.get("filter_profile") or ""
        if filter_profile:
            strategy_block += f" [{filter_profile}]"
        parts.append(strategy_block)
    return parts
