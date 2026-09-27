"""Unit-тесты шины событий: доставка, фильтрация, изоляция ошибок."""

from pathlib import Path

import pytest



from src.events import Event, EventBus
from src.events.types import ALL_EVENT_TYPES, TRADING_EVENT_TYPES, EventType, parse_event_type
from src.notifier.channel import Channel


class _Recorder(Channel):
    name = "recorder"
    supported_types = ALL_EVENT_TYPES

    def __init__(self, types=None) -> None:
        super().__init__(supported_types=types)
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)


class _Failing(Channel):
    name = "failing"
    supported_types = ALL_EVENT_TYPES

    def handle(self, event: Event) -> None:
        raise RuntimeError("канал сломан")


def test_publish_reaches_every_subscriber_in_order() -> None:
    bus = EventBus()
    first, second = _Recorder(), _Recorder()
    bus.subscribe(first)
    bus.subscribe(second)

    bus.publish(Event.heartbeat(tick_count=1, error_count=0))

    assert [e.type for e in first.events] == [EventType.HEARTBEAT]
    assert [e.type for e in second.events] == [EventType.HEARTBEAT]


def test_subscribe_returns_subscriber_and_ignores_duplicate() -> None:
    bus = EventBus()
    recorder = _Recorder()

    assert bus.subscribe(recorder) is recorder
    bus.subscribe(recorder)

    assert bus.subscribers == (recorder,)


def test_subscribe_rejects_object_without_handle() -> None:
    bus = EventBus()

    try:
        bus.subscribe(object())  # type: ignore[arg-type]
    except TypeError as exc:
        assert "handle()" in str(exc)
    else:
        raise AssertionError("объект без handle() не должен подписываться")


def test_subscriber_receives_only_supported_types() -> None:
    bus = EventBus()
    trading = _Recorder(TRADING_EVENT_TYPES)
    bus.subscribe(trading)

    bus.publish(Event.heartbeat(tick_count=1, error_count=0))
    bus.publish(Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2))

    assert [e.type for e in trading.events] == [EventType.SIGNAL]


def test_failing_subscriber_does_not_stop_delivery() -> None:
    bus = EventBus()
    survivor = _Recorder()
    bus.subscribe(_Failing())
    bus.subscribe(survivor)

    bus.publish(Event.error(operation="тик"))

    assert [e.type for e in survivor.events] == [EventType.ERROR]


def test_event_is_immutable_and_payload_is_read_only() -> None:
    event = Event.heartbeat(tick_count=2, error_count=0)

    try:
        event.payload["tick_count"] = 99  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("payload события должен быть неизменяемым")

    try:
        event.type = EventType.ERROR  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("событие должно быть неизменяемым")


def test_to_dict_is_json_serializable_shape() -> None:
    event = Event.decision(
        "NG-10.26", outcome="signal_buy", side="BUY", price=1234.5, strategy="macd", timeframe="15m",
    )

    data = event.to_dict()

    assert data["type"] == "decision"
    assert data["instrument"] == "NG-10.26"
    assert data["timeframe"] == "15m"
    assert data["bar_time"] is None
    assert data["payload"]["side"] == "BUY"


def test_catalog_has_nineteen_types_and_five_trading() -> None:
    assert len(EventType) == 19
    assert len(ALL_EVENT_TYPES) == 19
    assert TRADING_EVENT_TYPES == frozenset(
        {
            EventType.SIGNAL,
            EventType.TRADE_OPENED,
            EventType.STOP_HIT,
            EventType.TARGET_HIT,
            EventType.TRADE_CLOSED,
        }
    )


def test_parse_event_type_rejects_unknown_name() -> None:
    assert parse_event_type("signal") is EventType.SIGNAL
    try:
        parse_event_type("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("неизвестный тип события должен отвергаться")


BUS_MODULES = ("bus.py", "event.py", "types.py", "schema.py", "__init__.py")
FORBIDDEN = (
    "src.notifier",
    "src.trade_management",
    "src.broker",
    "src.strategies",
    "src.bot",
    "src.portfolio",
)


@pytest.mark.parametrize("filename", BUS_MODULES)
def test_bus_mechanism_does_not_import_domain_modules(filename: str) -> None:
    source = (Path(__file__).resolve().parents[3] / "src" / "events" / filename).read_text(
        encoding="utf-8"
    )

    imported = [
        name
        for name in FORBIDDEN
        if f"from {name}" in source or f"import {name}" in source
    ]

    assert not imported, f"{filename}: механизм шины зависит от {imported}"
