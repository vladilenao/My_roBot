"""Событие исполнителя, предназначенное для уведомлений.

Событие структурировано: тип из каталога, время, сделка и набор полей. Текста
здесь нет по условию — русскую фразу собирает шаблон канала, поэтому смысл
события читается по типу, а не по разбору сообщения.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from src.events.types import EventType


@dataclass(frozen=True)
class BrokerEvent:
    """Структурный факт об исполнении: заявка, сделка, отмена, клиринг."""

    type: EventType
    ts: datetime
    trade_id: str = ""
    instrument: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType(self.type))
        object.__setattr__(
            self, "payload", MappingProxyType(dict(self.payload or {}))
        )
