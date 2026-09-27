"""Форма payload каждого типа события.

Контракт хранится в коде рядом с событием, а не в разметке: тест сверяет
эти словари с конструкторами ``Event``, поэтому расхождение ломает сборку.
Описание для человека — в ``docs/notification/event-schemas.md``.
"""

from __future__ import annotations

from typing import Any

from src.events.types import EventType

_REQUIRED: dict[EventType, tuple[str, ...]] = {
    EventType.DECISION: ("outcome", "side"),
    EventType.SIGNAL: ("side", "quantity", "entry", "stop"),
    EventType.REJECTED: ("reason",),
    EventType.HEARTBEAT: ("tick_count", "error_count"),
    EventType.ERROR: ("operation",),
}

_OPTIONAL: dict[EventType, tuple[str, ...]] = {
    EventType.DECISION: ("price", "strategy", "filter_profile", "filtered_out", "event_id"),
    EventType.SIGNAL: ("targets", "expected_r", "strategy", "filter_profile", "trade_id"),
    EventType.REJECTED: ("code", "side", "price", "strategy", "filter_profile"),
    EventType.HEARTBEAT: (),
    EventType.ERROR: ("message",),
    EventType.RATE_LIMITED: ("source",),
    EventType.CLEARING_DONE: (),
}

_EXECUTION_OPTIONAL: tuple[str, ...] = (
    "side",
    "quantity",
    "price",
    "stop",
    "take_profit",
    "pnl",
    "fee",
    "order_id",
    "execution_id",
    "status",
    "reason",
    "occurred_at",
)

REQUIRED_PAYLOAD_FIELDS: dict[EventType, tuple[str, ...]] = {
    **_REQUIRED,
    EventType.ORDER_ACCEPTED: ("trade_id",),
    EventType.ORDER_REJECTED: ("trade_id",),
    EventType.TRADE_OPENED: ("trade_id",),
    EventType.POSITION_ADDED: ("trade_id",),
    EventType.STOP_HIT: ("trade_id",),
    EventType.TARGET_HIT: ("trade_id",),
    EventType.TRADE_CLOSED: ("trade_id",),
    EventType.TRADE_CANCELLED: ("trade_id",),
    EventType.PROTECTION_ARMED: ("trade_id",),
    EventType.STOP_MOVED: ("trade_id",),
    EventType.RISK_LIMIT_HIT: ("trade_id",),
    EventType.RESERVATION_CHANGED: ("trade_id",),
    EventType.CLEARING_DONE: (),
    EventType.RATE_LIMITED: (),
}

OPTIONAL_PAYLOAD_FIELDS: dict[EventType, tuple[str, ...]] = {
    **_OPTIONAL,
    **{
        event_type: _EXECUTION_OPTIONAL
        for event_type in (
            EventType.ORDER_ACCEPTED,
            EventType.ORDER_REJECTED,
            EventType.TRADE_OPENED,
            EventType.POSITION_ADDED,
            EventType.STOP_HIT,
            EventType.TARGET_HIT,
            EventType.TRADE_CLOSED,
            EventType.TRADE_CANCELLED,
            EventType.PROTECTION_ARMED,
            EventType.STOP_MOVED,
            EventType.RISK_LIMIT_HIT,
            EventType.RESERVATION_CHANGED,
        )
    },
    EventType.CLEARING_DONE: ("balance", "positions"),
}


_JSON_TYPES: dict[str, str] = {
    "quantity": "number",
    "positions": "number",
    "tick_count": "number",
    "error_count": "number",
    "filtered_out": "boolean",
    "targets": "array",
}


def _property(name: str) -> dict[str, Any]:
    """Одно свойство схемы.

    ``Decimal`` сериализуется строкой, поэтому цены, цели, комиссия и
    ожидаемый результат — строки, а не числа: иначе теряются знаки и точность.
    """
    json_type = _JSON_TYPES.get(name, "string")
    if json_type == "array":
        return {"type": "array", "items": {"type": "string"}}
    return {"type": json_type}


def payload_schema(event_type: EventType) -> dict[str, Any]:
    """JSON Schema payload одного типа события.

    Единственный источник правды: ``docs/notification/event-schemas.md``
    перепечатывает эти схемы, а тест сверяет документ с этим кодом.
    """
    required = REQUIRED_PAYLOAD_FIELDS[event_type]
    optional = OPTIONAL_PAYLOAD_FIELDS[event_type]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"payload {event_type.value}",
        "type": "object",
        "required": list(required),
        "properties": {name: _property(name) for name in (*required, *optional)},
        "additionalProperties": False,
    }
