"""Порт доставки уведомлений.

Канал ничего не знает о торговле: он получает готовое событие, проверяет
свой ``supported_types`` и сам решает, что показать пользователю.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from src.events.event import Event
from src.events.types import EventType


class Channel(ABC):
    """Абстрактный канал доставки событий подписчику."""

    name: str = ""
    notification_types: frozenset[EventType] = frozenset()
    control_types: frozenset[EventType] = frozenset()
    supported_types: frozenset[EventType] = frozenset()

    def __init__(
        self,
        *,
        supported_types: Iterable[EventType] | None = None,
    ) -> None:
        configured = (
            frozenset(supported_types)
            if supported_types is not None
            else self.notification_types or self.supported_types
        )
        if not configured:
            raise ValueError(
                f"Канал '{self.name or type(self).__name__}' должен объявить "
                f"supported_types: пустой набор означает, что доставлять нечего"
            )
        self.notification_types = configured
        self.supported_types = configured | self.control_types

    def accepts(self, event: Event) -> bool:
        return event.type in self.supported_types

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Доставить событие подписчику."""

    def close(self) -> None:
        """Освободить ресурсы канала. Для синхронных каналов — ничего."""
