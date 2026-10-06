"""Схема payload обязана совпадать с кодом и с документом: расхождение ломает сборку.

Три источника правды проверяются друг против друга: конструкторы ``Event``, словари
``src/events/schema.py`` и блоки ``json`` в ``docs/notification/event-schemas.md``.
Валидатор схем намеренно самописный и минимальный: неизвестная конструкция в схеме
должна валить тест, а не молча игнорироваться.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from src.events.event import Event
from src.events.schema import (
    OPTIONAL_PAYLOAD_FIELDS,
    REQUIRED_PAYLOAD_FIELDS,
    payload_schema,
)
from src.events.types import ALL_EVENT_TYPES, EventType

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "notification" / "event-schemas.md"

ALLOWED_SCHEMA_KEYS = {
    "$schema",
    "title",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "items",
}

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": (list, tuple),
    "string": str,
    "boolean": bool,
    "null": type(None),
}

BLOCK_PATTERN = re.compile(
    r"^### `(?P<name>[a-z_]+)`\s*\n(?:.*?\n)*?```json\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def _document_schemas() -> dict[str, dict]:
    text = DOC_PATH.read_text(encoding="utf-8")
    found = {match["name"]: json.loads(match["body"]) for match in BLOCK_PATTERN.finditer(text)}
    return found


def _validate(instance, schema, path: str = "payload") -> None:
    unknown = set(schema) - ALLOWED_SCHEMA_KEYS
    assert not unknown, f"{path}: схема использует неподдерживаемые конструкции {sorted(unknown)}"

    expected = schema.get("type")
    if expected in ("number", "integer"):
        assert isinstance(instance, (int, float)) and not isinstance(instance, bool), (
            f"{path}: ожидалось {expected}, получено {type(instance).__name__}"
        )
    elif expected is not None:
        assert isinstance(instance, _JSON_TYPES[expected]), (
            f"{path}: ожидалось {expected}, получено {type(instance).__name__}"
        )

    if expected == "object":
        for name in schema.get("required", ()):
            assert name in instance, f"{path}: нет обязательного поля {name!r}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(properties)
            assert not extra, f"{path}: поля вне схемы {sorted(extra)}"
        for name, value in instance.items():
            if name in properties:
                _validate(value, properties[name], f"{path}.{name}")

    if expected == "array" and "items" in schema:
        for index, item in enumerate(instance):
            _validate(item, schema["items"], f"{path}[{index}]")


def _samples() -> dict[EventType, Event]:
    common = {"trade_id": "trade-1", "order_id": "o-1", "execution_id": "e-1",
              "status": "fill", "quantity": 2, "price": Decimal("100.5"),
              "fee": Decimal("1"), "reason": "confirmed", "side": "BUY"}
    return {
        EventType.DECISION: Event.decision("NG-10.26", outcome="signal_buy", side="BUY"),
        EventType.SIGNAL: Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2),
        EventType.REJECTED: Event.rejected("NG-10.26", reason="мало"),
        EventType.ORDER_ACCEPTED: Event.broker_event(EventType.ORDER_ACCEPTED, **common),
        EventType.ORDER_REJECTED: Event.broker_event(EventType.ORDER_REJECTED, **common),
        EventType.TRADE_OPENED: Event.broker_event(EventType.TRADE_OPENED, **common),
        EventType.POSITION_ADDED: Event.broker_event(EventType.POSITION_ADDED, **common),
        EventType.STOP_HIT: Event.broker_event(EventType.STOP_HIT, **common),
        EventType.TARGET_HIT: Event.broker_event(EventType.TARGET_HIT, **common),
        EventType.TRADE_CLOSED: Event.broker_event(EventType.TRADE_CLOSED, **common),
        EventType.TRADE_CANCELLED: Event.broker_event(EventType.TRADE_CANCELLED, **common),
        EventType.PROTECTION_ARMED: Event.broker_event(EventType.PROTECTION_ARMED, **common),
        EventType.STOP_MOVED: Event.broker_event(EventType.STOP_MOVED, **common),
        EventType.RISK_LIMIT_HIT: Event.broker_event(EventType.RISK_LIMIT_HIT, **common),
        EventType.RESERVATION_CHANGED: Event.broker_event(EventType.RESERVATION_CHANGED, **common),
        EventType.CLEARING_DONE: Event.clearing_done(balance=Decimal("1000.5"), positions=2),
        EventType.HEARTBEAT: Event.heartbeat(tick_count=1, error_count=0),
        EventType.ERROR: Event.error(operation="тик"),
        EventType.RATE_LIMITED: Event.rate_limited(source="tinkoff"),
    }


def test_schema_covers_every_event_type() -> None:
    documented = set(REQUIRED_PAYLOAD_FIELDS) | set(OPTIONAL_PAYLOAD_FIELDS)

    assert documented == set(ALL_EVENT_TYPES), (
        "в схеме и в каталоге разные наборы типов: "
        f"нет в схеме {set(ALL_EVENT_TYPES) - documented}, "
        f"лишнее в схеме {documented - set(ALL_EVENT_TYPES)}"
    )


@pytest.mark.parametrize("event_type", sorted(ALL_EVENT_TYPES, key=lambda item: item.value))
def test_sample_event_carries_required_fields(event_type: EventType) -> None:
    event = _samples()[event_type]

    missing = set(REQUIRED_PAYLOAD_FIELDS.get(event_type, ())) - set(event.payload)

    assert not missing, f"{event_type.value}: в payload нет обязательных полей {sorted(missing)}"


@pytest.mark.parametrize("event_type", sorted(ALL_EVENT_TYPES, key=lambda item: item.value))
def test_payload_has_no_fields_outside_schema(event_type: EventType) -> None:
    event = _samples()[event_type]
    allowed = set(REQUIRED_PAYLOAD_FIELDS.get(event_type, ())) | set(
        OPTIONAL_PAYLOAD_FIELDS.get(event_type, ())
    )

    extra = set(event.payload) - allowed

    assert not extra, f"{event_type.value}: в payload есть поля вне схемы {sorted(extra)}"


@pytest.mark.parametrize("event_type", sorted(ALL_EVENT_TYPES, key=lambda item: item.value))
def test_to_dict_is_json_serializable(event_type: EventType) -> None:
    json.dumps(_samples()[event_type].to_dict(), ensure_ascii=False)


def test_document_schemas_match_code() -> None:
    documented = _document_schemas()

    assert set(documented) == {event_type.value for event_type in ALL_EVENT_TYPES}, (
        "в документе не те типы, что в каталоге: "
        f"нет {sorted({t.value for t in ALL_EVENT_TYPES} - set(documented))}, "
        f"лишние {sorted(set(documented) - {t.value for t in ALL_EVENT_TYPES})}"
    )

    for event_type in ALL_EVENT_TYPES:
        assert documented[event_type.value] == payload_schema(event_type), (
            f"{event_type.value}: блок json в документе разошёлся с src/events/schema.py"
        )


@pytest.mark.parametrize("event_type", sorted(ALL_EVENT_TYPES, key=lambda item: item.value))
def test_sample_event_matches_documented_schema(event_type: EventType) -> None:
    payload = _samples()[event_type].to_dict()["payload"]

    _validate(payload, _document_schemas()[event_type.value])


def test_unfilled_optional_field_is_absent_not_null() -> None:
    event = Event.broker_event(
        EventType.ORDER_ACCEPTED, trade_id="t-1", side="BUY", quantity=1,
    )

    assert "price" not in event.payload
    assert "price" not in event.to_dict()["payload"]


def test_amounts_serialize_as_decimal_strings() -> None:
    event = Event.signal("NG-10.26", side="BUY", quantity=1, entry=1234, stop=1200.5)

    payload = event.to_dict()["payload"]

    assert payload["entry"] == "1234"
    assert payload["stop"] == "1200.5"


def test_rich_admission_and_ownerless_portfolio_alert_match_schema():
    diagnostics = {"budget_base": Decimal(100000), "portfolio_pct": Decimal(2), "risk_budget": Decimal(2000),
                   "open_risk": Decimal(1200), "pending_risk": Decimal(120), "free_risk": Decimal(680),
                   "risk_excess": Decimal(0), "risk_state": "known", "requested_quantity": 5,
                   "selected_quantity": 2, "limiting_constraint": "margin"}
    signal = Event.signal("SBER", side="BUY", quantity=2, entry=100, stop=96, diagnostics=diagnostics,
                          fixed_reward_amount=160, fixed_quantity=2, net_reward_amount=120, algorithm_version="economics-v2")
    rejected = Event.rejected("SBER", reason="ниже порога", code="payoff-below-floor",
                             diagnostics={**diagnostics, "risk_amount": Decimal(100), "costs_amount": Decimal(40),
                                          "payoff_ratio": Decimal("1.499"), "threshold": Decimal("1.5"), "algorithm_version": "economics-v2"})
    alert = Event.broker_event(EventType.RISK_LIMIT_HIT, risk_scope="portfolio", **diagnostics)
    for event in (signal, rejected, alert):
        _validate(event.to_dict()["payload"], payload_schema(event.type))
    assert "trade_id" not in alert.payload
    assert rejected.to_dict()["payload"]["payoff_ratio"] == "1.499"


def test_canonical_execution_money_matches_schema():
    event = Event.broker_event(EventType.TRADE_CLOSED, trade_id="trade", quantity=5, price=106, fee=0,
                              fee_source="broker", gross_pnl=60, fees_total=Decimal("22.5"), net_pnl=Decimal("37.5"),
                              fees_known=True, pnl_units="RUB", quantity_remaining=0)
    _validate(event.to_dict()["payload"], payload_schema(event.type))
    assert event.to_dict()["payload"]["fee"] == "0"
