"""Шаблон событий исполнения: текст собирается здесь, а не в исполнителе.

Каждому типу из каталога соответствует одна формулировка. Исполнитель приносит
структурные поля, поэтому смысл события читается по типу, а не по разбору
сообщения. Идентификатор сделки в текст не попадает: он нужен для корреляции,
но пользователю ничего не объясняет.
"""

from __future__ import annotations

from typing import Callable

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates.common import amount, budget_text, financial_text, pnl_text, price

PREFIXES = {
    EventType.ORDER_ACCEPTED: "📝 Ордер",
    EventType.ORDER_REJECTED: "📝 Ордер",
    EventType.TRADE_OPENED: "💰 Сделка",
    EventType.POSITION_ADDED: "➕ Добор",
    EventType.STOP_HIT: "🛑 Стоп",
    EventType.TARGET_HIT: "🎯 Цель",
    EventType.TRADE_CLOSED: "💰 Сделка",
    EventType.TRADE_CANCELLED: "❌ Отмена",
    EventType.CLEARING_DONE: "🏛 Клиринг",
    EventType.RISK_LIMIT_HIT: "⚠️ Риск",
    EventType.PROTECTION_ARMED: "🛡 Защита",
}

DEFAULT_PREFIX = "📌 Событие"


def _order_accepted(event: Event) -> str:
    return (
        f"Заявка {event.get('side')} {event.get('quantity')} {event.instrument} "
        f"по {amount(event.get('price'))} принята (id={event.get('order_id')})"
    )


def _order_rejected(event: Event) -> str:
    return f"Сделка {event.get('side')} {event.instrument} отклонена ({event.get('reason')})"


def _trade_opened(event: Event) -> str:
    return f"Вход {event.get('side')} {event.get('quantity')} {event.instrument} по {amount(event.get('price'))}"


def _position_added(event: Event) -> str:
    text = f"Добор {event.get('side')} {event.get('quantity')} {event.instrument} по {amount(event.get('price'))}"
    if event.get("requested_quantity") is not None:
        labels = {"risk": "риск", "margin": "ГО", "max-quantity": "предел количества", "profile": "профиль"}
        constraint = event.get("limiting_constraint")
        text += (f"; запрошено {event.get('requested_quantity')}, выбрано {event.get('selected_quantity')}"
                 + (f", ограничение: {labels.get(constraint, constraint)}" if constraint else ""))
    return text


def _target_hit(event: Event) -> str:
    return f"Цель {event.get('quantity')} {event.instrument} по {amount(event.get('price'))}"


def _stop_hit(event: Event) -> str:
    return f"Защитный стоп {event.get('quantity')} {event.instrument} по {amount(event.get('price'))}"


def _trade_closed(event: Event) -> str:
    if event.get("gross_pnl") is not None:
        return (f"Выход {event.get('quantity')} {event.instrument} по {amount(event.get('price'))}, "
                f"остаток {event.get('quantity_remaining')}")
    pnl = pnl_text(event.get("pnl"))
    if event.get("reason"):
        return (
            f"Закрытие позиции {event.instrument} {event.get('quantity')} шт: "
            f"PnL {pnl} руб"
        )
    return (
        f"Закрытие {event.get('quantity')} {event.instrument} "
        f"по {amount(event.get('price'))} (PnL {pnl})"
    )


def _trade_cancelled(event: Event) -> str:
    if event.get("reason") == "ttl":
        return f"Заявка {event.get('order_id')} истекла по TTL"
    return f"Заявка {event.get('order_id')} отменена ({event.get('reason')})"


def _protection_armed(event: Event) -> str:
    return (
        f"Защитный стоп {amount(event.get('stop'))} / "
        f"ТП {amount(event.get('take_profit'))} установлен"
    )


def _risk_limit_hit(event: Event) -> str:
    budget = budget_text(event)
    return f"{budget}; новые входы/доборы запрещены" if budget else "Лимит риска достигнут — требуется проверка общего бюджета"


def _clearing_done(event: Event) -> str:
    return (
        f"снимок баланса {pnl_text(event.get('balance'))} руб, "
        f"открыто позиций: {event.get('positions')}"
    )


_TEXTS: dict[EventType, Callable[[Event], str]] = {
    EventType.ORDER_ACCEPTED: _order_accepted,
    EventType.ORDER_REJECTED: _order_rejected,
    EventType.TRADE_OPENED: _trade_opened,
    EventType.POSITION_ADDED: _position_added,
    EventType.TARGET_HIT: _target_hit,
    EventType.STOP_HIT: _stop_hit,
    EventType.TRADE_CLOSED: _trade_closed,
    EventType.TRADE_CANCELLED: _trade_cancelled,
    EventType.PROTECTION_ARMED: _protection_armed,
    EventType.RISK_LIMIT_HIT: _risk_limit_hit,
    EventType.CLEARING_DONE: _clearing_done,
}


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    """Фраза по структурным полям или ``None``, если у типа нет представления."""
    build = _TEXTS.get(event.type)
    if build is None:
        return None
    text = f"{PREFIXES.get(event.type, DEFAULT_PREFIX)}: {build(event)}"
    financial = financial_text(event)
    return text + f"; {financial}" if financial else text


def render_price(value) -> str:
    return price(value)
