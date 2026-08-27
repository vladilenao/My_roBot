from __future__ import annotations

from abc import ABC, abstractmethod


class ExecutionPort(ABC):
    """Выходной слой принятых решений робота.

    Единая точка, в которую оркестратор отдаёт решение. Текущая реализация
    уведомляет трейдера; будущая — исполняет торговые ордера.
    """

    @abstractmethod
    def execute(self, decision, instrument) -> None:
        """Принимает решение по инструменту и доставляет его наружу."""
        raise NotImplementedError


class NotifyOnlyExecutionPort(ExecutionPort):
    """Доставляет решение трейдеру через нотификатор, не выполняя сделок."""

    def __init__(self, notifier) -> None:
        self._notifier = notifier

    def execute(self, decision, instrument) -> None:
        label = getattr(instrument, "label", "")
        self._notifier.notify_decision(decision, label)
