"""Шаблон решений стратегии и отказов допуска."""

from __future__ import annotations

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates.common import budget_text, header_parts, price


def _signal_text(event: Event) -> str:
    if event.get("filtered_out"):
        return "❌ Отклонено фильтром."
    side = event.get("side") or ""
    value = event.get("price")
    shown = str(round(value, 3)) if value is not None else "—"
    if side == "BUY":
        return f"🟢 ПОКУПКА (BUY) — Цена: {shown}"
    if side == "SELL":
        return f"🔴 ПРОДАЖА (SELL) — Цена: {shown}"
    return "⏳ Нет сигнала."


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    if event.type is EventType.DECISION:
        parts = header_parts(event, "●", tz_offset_hours)
        return " ".join(parts) + f" ➜ {_signal_text(event)}"
    if event.type is EventType.REJECTED:
        parts = header_parts(event, "⛔", tz_offset_hours)
        text = " ".join(parts) + f" ➜ Сделка не допущена: {event.get('reason') or ''}"
        numbers = [f"{label}={price(event.get(key))}{units}" for key, label, units in (
            ("risk_amount", "R", " ₽"), ("costs_amount", "C", " ₽"), ("slippage_amount", "S", " ₽"),
            ("payoff_ratio", "payoff", ""), ("threshold", "порог", "")) if event.get(key) is not None]
        if numbers:
            text += " (" + ", ".join(numbers) + ")"
        budget = budget_text(event)
        return text + f"; {budget}" if budget else text
    return None
