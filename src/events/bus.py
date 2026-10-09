"""Шина событий: хранит подписки и раздаёт события подписчикам.

Шина не знает ни о торговле, ни о каналах, ни о шаблонах: это общий механизм
обмена. Семантика доставки — «не более одного раза»: исключение одного
подписчика фиксируется в логе и не мешает остальным.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from src.events.event import Event
from src.events.types import EventType
from src.logging_setup import get_logger

log = get_logger(__name__)


@runtime_checkable
class Subscriber(Protocol):
    """Подписчик, умеющий обработать событие."""

    supported_types: frozenset[EventType]

    def handle(self, event: Event) -> None: ...


class EventBus:
    """Синхронная раздача событий подписчикам в порядке подписки."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    @property
    def subscribers(self) -> tuple[Subscriber, ...]:
        return tuple(self._subscribers)

    def subscribe(self, subscriber: Subscriber) -> Subscriber:
        """Подписать подписчика; порядок подписки задаёт порядок доставки."""
        if not isinstance(subscriber, Subscriber):
            raise TypeError(
                f"Подписчик должен реализовать handle() и supported_types, "
                f"получено {type(subscriber).__name__}"
            )
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)
        return subscriber

    def subscribe_all(self, subscribers: Iterable[Subscriber]) -> None:
        for subscriber in subscribers:
            self.subscribe(subscriber)

    def publish(self, event: Event) -> None:
        """Раздать событие всем подписчикам, подписанным на его тип."""
        for subscriber in self._subscribers:
            if event.type not in subscriber.supported_types:
                continue
            try:
                subscriber.handle(event)
            except Exception:
                log.exception(
                    "Подписчик %s не обработал событие %s.",
                    type(subscriber).__name__,
                    event.type.value,
                )

    def clear(self) -> None:
        self._subscribers.clear()
