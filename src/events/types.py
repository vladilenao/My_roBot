"""Каталог типов событий шины.

Список закрыт: 19 типов, каждый с фиксированным набором полей payload.
Значения ``StrEnum`` совпадают с именами типов в
``docs/notification/event-schemas.md`` и используются в конфигурации
``[notifier.<канал>] events``.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """Тип события шины."""

    DECISION = "decision"
    SIGNAL = "signal"
    TRADE_OPENED = "trade_opened"
    POSITION_ADDED = "position_added"
    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    TRADE_CLOSED = "trade_closed"
    REJECTED = "rejected"
    TRADE_CANCELLED = "trade_cancelled"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    PROTECTION_ARMED = "protection_armed"
    STOP_MOVED = "stop_moved"
    RISK_LIMIT_HIT = "risk_limit_hit"
    RESERVATION_CHANGED = "reservation_changed"
    CLEARING_DONE = "clearing_done"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"


TRADING_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SIGNAL,
        EventType.TRADE_OPENED,
        EventType.STOP_HIT,
        EventType.TARGET_HIT,
        EventType.TRADE_CLOSED,
    }
)

ALL_EVENT_TYPES: frozenset[EventType] = frozenset(EventType)

EVENT_TYPE_NAMES: tuple[str, ...] = tuple(sorted(event.value for event in EventType))


def parse_event_type(value: str | EventType) -> EventType:
    """Вернуть тип события по имени; неизвестное имя отвергается."""
    if isinstance(value, EventType):
        return value
    try:
        return EventType(value)
    except ValueError as exc:
        raise ValueError(
            f"Неизвестный тип события '{value}'. Допустимые: {', '.join(EVENT_TYPE_NAMES)}"
        ) from exc


def parse_event_types(values: object) -> tuple[EventType, ...]:
    """Разобрать список имён типов событий из конфигурации."""
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError(
            "Список типов событий должен быть массивом строк, "
            f"получено {type(values).__name__}"
        )
    parsed = tuple(parse_event_type(value) for value in values)
    if not parsed:
        raise ValueError("Список типов событий не может быть пустым")
    return parsed
