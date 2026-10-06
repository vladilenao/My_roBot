"""Форма payload каждого типа события.

Контракт хранится в коде рядом с событием, а не в разметке: тест сверяет
эти словари с конструкторами ``Event``, поэтому расхождение ломает сборку.
Описание для человека — в ``docs/notification/event-schemas.md``.
"""

from __future__ import annotations

from typing import Any

from src.events.types import EventType
from src.events.visual import SCHEMA_ID

DIAGNOSTIC_PAYLOAD_FIELDS = (
    "budget_base", "portfolio_pct", "risk_budget", "open_risk", "pending_risk", "free_risk", "risk_excess",
    "risk_state", "unknown_reason", "requested_quantity", "selected_quantity", "limiting_constraint",
)
FINANCIAL_PAYLOAD_FIELDS = (
    "gross_pnl", "net_pnl", "fees_total", "fees_known", "fee_source", "pnl_units", "quantity_remaining",
)

_REQUIRED: dict[EventType, tuple[str, ...]] = {
    EventType.DECISION: ("outcome", "side"),
    EventType.SIGNAL: ("side", "quantity", "entry", "stop"),
    EventType.REJECTED: ("reason",),
    EventType.HEARTBEAT: ("tick_count", "error_count"),
    EventType.ERROR: ("operation",),
}

_OPTIONAL: dict[EventType, tuple[str, ...]] = {
    EventType.DECISION: ("price", "strategy", "filter_profile", "filtered_out", "event_id"),
    EventType.SIGNAL: (
        "targets", "expected_r", "strategy", "filter_profile", "trade_id",
        "risk_amount", "reward_amount", "costs_amount", "payoff_ratio",
        "fixed_reward_amount", "fixed_quantity", "net_reward_amount", "algorithm_version",
        *DIAGNOSTIC_PAYLOAD_FIELDS,
    ),
    EventType.REJECTED: ("code", "side", "price", "strategy", "filter_profile", "algorithm_version",
                         "risk_amount", "costs_amount", "slippage_amount", "payoff_ratio", "threshold",
                         *DIAGNOSTIC_PAYLOAD_FIELDS),
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
    EventType.RISK_LIMIT_HIT: (),
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
    EventType.RISK_LIMIT_HIT: (*_EXECUTION_OPTIONAL, "trade_id", "risk_scope", *DIAGNOSTIC_PAYLOAD_FIELDS),
}
for _event_type in (EventType.TRADE_OPENED, EventType.POSITION_ADDED, EventType.STOP_HIT, EventType.TARGET_HIT, EventType.TRADE_CLOSED):
    OPTIONAL_PAYLOAD_FIELDS[_event_type] += FINANCIAL_PAYLOAD_FIELDS
OPTIONAL_PAYLOAD_FIELDS[EventType.POSITION_ADDED] += ("requested_quantity", "selected_quantity", "limiting_constraint")
for _event_type in (EventType.SIGNAL, EventType.TRADE_OPENED, EventType.POSITION_ADDED, EventType.STOP_HIT,
                   EventType.TARGET_HIT, EventType.TRADE_CLOSED, EventType.STOP_MOVED,
                   EventType.TRADE_CANCELLED, EventType.ORDER_REJECTED):
    OPTIONAL_PAYLOAD_FIELDS[_event_type] += ("visual",)


_JSON_TYPES: dict[str, str] = {
    "quantity": "number",
    "positions": "number",
    "tick_count": "number",
    "error_count": "number",
    "filtered_out": "boolean",
    "targets": "array",
    "requested_quantity": "number",
    "selected_quantity": "number",
    "fixed_quantity": "number",
    "quantity_remaining": "number",
    "fees_known": "boolean",
}


def _property(name: str) -> dict[str, Any]:
    """Одно свойство схемы.

    ``Decimal`` сериализуется строкой, поэтому цены, цели, комиссия и
    ожидаемый результат — строки, а не числа: иначе теряются знаки и точность.
    """
    if name == "visual":
        return {"$ref": SCHEMA_ID}
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
