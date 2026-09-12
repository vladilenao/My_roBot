from __future__ import annotations

from abc import ABC, abstractmethod

from src.logging_setup import get_logger

log = get_logger(__name__)


class ExecutionPort(ABC):
    """Выходной слой принятых решений робота.

    Единая точка, в которую оркестратор отдаёт решение. Текущая реализация
    уведомляет трейдера; будущая — исполняет торговые ордера.
    """

    @abstractmethod
    def execute(
        self,
        decision,
        instrument,
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> None:
        """Принимает решение по инструменту и доставляет его наружу."""
        raise NotImplementedError


class NotifyOnlyExecutionPort(ExecutionPort):
    """Доставляет решение трейдеру через нотификатор, не выполняя сделок."""

    def __init__(self, notifier) -> None:
        self._notifier = notifier

    def execute(
        self,
        decision,
        instrument,
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> None:
        label = getattr(instrument, "short_name", None) or getattr(instrument, "label", "")
        self._notifier.notify_decision(
            decision, label,
            filter_profile=filter_profile, filtered_out=filtered_out,
            timeframe=timeframe,
        )
