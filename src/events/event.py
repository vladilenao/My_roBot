"""Неизменяемое событие шины и конструкторы для каждого типа из каталога.

Событие самодостаточно: всё, что нужно для представления, лежит в нём самом,
поэтому получателю не требуется обращаться к базе, брокеру или конфигурации.
Сериализуемая форма описана в ``docs/notification/event-schemas.md``, её источник
правды — ``src/events/schema.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from src.events.types import EventType

_EXECUTION_AMOUNTS = frozenset(
    {"price", "stop", "take_profit", "pnl", "fee", "balance"}
)


def _freeze(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Сделать payload неизменяемым и убрать незаполненные необязательные поля.

    ``None`` означает «поля нет»: незаполненная цена в JSON не должна выглядеть как
    ``null``, потому что получатель читает отсутствие ключа иначе, чем значение.
    """
    if not payload:
        return MappingProxyType({})
    return MappingProxyType({key: value for key, value in payload.items() if value is not None})


def _amount(value: Any) -> Any:
    """Привести цену, цель или сумму к ``Decimal``.

    В сериализованной форме это строка: так не теряются ни знаки, ни точность,
    а схема payload остаётся исполнимым контрактом, а не пожеланием.
    """
    if value is None or isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _text(value: Any) -> Any:
    """Привести значение к JSON-совместимому виду.

    Контейнеры разворачиваются рекурсивно: ``targets`` — это кортеж значений, и в
    сериализованной форме он должен быть массивом, а не строкой repr.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_text(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _text(item) for key, item in value.items()}
    return str(value)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        return to_pydatetime()
    raise TypeError(f"Не удалось привести {type(value).__name__} к datetime")


@dataclass(frozen=True)
class Event:
    """Событие шины: тип, контекст и неизменяемый payload."""

    type: EventType
    instrument: str = ""
    bar_time: datetime | None = None
    timeframe: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "bar_time", _as_datetime(self.bar_time))
        object.__setattr__(self, "type", EventType(self.type))

    def get(self, key: str, default: Any = None) -> Any:
        """Значение payload или запасное значение, если поля нет."""
        return self.payload.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Сериализуемая форма, соответствующая JSON Schema типа."""
        return {
            "type": self.type.value,
            "instrument": self.instrument,
            "bar_time": self.bar_time.isoformat() if self.bar_time else None,
            "timeframe": self.timeframe,
            "payload": {key: _text(value) for key, value in self.payload.items()},
        }

    @classmethod
    def _make(cls, event_type: EventType, **kwargs: Any) -> "Event":
        return cls(type=event_type, **kwargs)

    @classmethod
    def decision(
        cls,
        instrument: str,
        *,
        outcome: str,
        side: str,
        price: Any = None,
        strategy: str = "",
        filter_profile: str = "",
        filtered_out: bool = False,
        event_id: str = "",
        bar_time: Any = None,
        timeframe: str = "",
    ) -> "Event":
        """Исход анализа по контракту за тик."""
        return cls._make(
            EventType.DECISION,
            instrument=instrument,
            bar_time=bar_time,
            timeframe=timeframe,
            payload={
                "outcome": outcome,
                "side": side,
                "price": _amount(price),
                "strategy": strategy,
                "filter_profile": filter_profile,
                "filtered_out": filtered_out,
                "event_id": event_id,
            },
        )

    @classmethod
    def signal(
        cls,
        instrument: str,
        *,
        side: str,
        quantity: int,
        entry: Any,
        stop: Any,
        targets: tuple[Any, ...] = (),
        expected_r: Any = None,
        strategy: str = "",
        filter_profile: str = "",
        bar_time: Any = None,
        timeframe: str = "",
        trade_id: str = "",
    ) -> "Event":
        """Рекомендация: план допущен и уйдёт в заявку, но ещё не исполнен."""
        return cls._make(
            EventType.SIGNAL,
            instrument=instrument,
            bar_time=bar_time,
            timeframe=timeframe,
            payload={
                "side": side,
                "quantity": quantity,
                "entry": _amount(entry),
                "stop": _amount(stop),
                "targets": tuple(_amount(target) for target in targets),
                "expected_r": _amount(expected_r),
                "strategy": strategy,
                "filter_profile": filter_profile,
                "trade_id": trade_id,
            },
        )

    @classmethod
    def rejected(
        cls,
        instrument: str,
        *,
        reason: str,
        code: str = "",
        side: str = "",
        price: Any = None,
        strategy: str = "",
        filter_profile: str = "",
        bar_time: Any = None,
        timeframe: str = "",
    ) -> "Event":
        """Недопуск сделки на допуске."""
        return cls._make(
            EventType.REJECTED,
            instrument=instrument,
            bar_time=bar_time,
            timeframe=timeframe,
            payload={
                "reason": reason,
                "code": code,
                "side": side,
                "price": _amount(price),
                "strategy": strategy,
                "filter_profile": filter_profile,
            },
        )

    @classmethod
    def broker_event(
        cls,
        event_type: EventType,
        *,
        trade_id: str = "",
        instrument: str = "",
        bar_time: Any = None,
        timeframe: str = "",
        **payload: Any,
    ) -> "Event":
        """Событие исполнителя: структурный факт без готового текста.

        Исполнитель сообщает, что произошло, а как это показать решает шаблон
        канала. ``trade_id`` остаётся внутри события для корреляции и в текст
        не выводится; у события клиринга нет ни сделки, ни контракта.
        """
        body = {
            key: _amount(value) if key in _EXECUTION_AMOUNTS else value
            for key, value in payload.items()
        }
        if trade_id:
            body["trade_id"] = trade_id
        return cls._make(
            event_type,
            instrument=instrument,
            bar_time=bar_time,
            timeframe=timeframe,
            payload=body,
        )

    @classmethod
    def clearing_done(cls, *, balance: Any = None, positions: int = 0, bar_time: Any = None) -> "Event":
        """Сверка счёта завершена: снимок баланса и число открытых позиций."""
        return cls._make(
            EventType.CLEARING_DONE,
            bar_time=bar_time,
            payload={"balance": _amount(balance), "positions": positions},
        )

    @classmethod
    def heartbeat(cls, *, tick_count: int, error_count: int) -> "Event":
        """Пульс робота по окончании тика."""
        return cls._make(
            EventType.HEARTBEAT,
            payload={"tick_count": tick_count, "error_count": error_count},
        )

    @classmethod
    def error(cls, *, operation: str, message: str = "") -> "Event":
        """Ошибка операции: пользователю достаётся название операции, не дамп."""
        return cls._make(
            EventType.ERROR,
            payload={"operation": operation, "message": message},
        )

    @classmethod
    def rate_limited(cls, *, source: str = "") -> "Event":
        """Исчерпан лимит запросов к брокеру; пользователю не показывается."""
        return cls._make(EventType.RATE_LIMITED, payload={"source": source})
