from __future__ import annotations

from abc import ABC, abstractmethod

from src.logging_setup import get_logger
from src.trade_management.models import TradePlan

log = get_logger(__name__)


class ExecutionPort(ABC):
    """Выходной слой принятых решений робота.

    Единая точка, в которую оркестратор отдаёт решение. Реализация:
    уведомление трейдера (`NotifyOnlyExecutionPort`).
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
    ) -> object | None:
        """Принимает решение по инструменту и доставляет его наружу.

        Возвращает объект результата сделки (например, `OrderResult`) либо None.
        """
        raise NotImplementedError

    @abstractmethod
    def report_rejection(
        self,
        decision,
        instrument,
        *,
        reason_message: str,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> None:
        """Доставляет уведомление о недопуске сделки с причиной."""
        raise NotImplementedError

    @abstractmethod
    def report_entry_accepted(
        self,
        plan,
        instrument,
        *,
        quantity: int = 0,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> None:
        """Доставляет уведомление о принятой в работу сделке (ждёт подтверждения брокера)."""
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
        label = _short_contract_name(instrument)
        if isinstance(decision, TradePlan):
            self._notifier.notify_plan(decision, label, timeframe=timeframe)
            return
        self._notifier.notify_decision(
            decision, label,
            filter_profile=filter_profile, filtered_out=filtered_out,
            timeframe=timeframe,
        )

    def report_rejection(
        self,
        decision,
        instrument,
        *,
        reason_message: str,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> None:
        label = _short_contract_name(instrument)
        self._notifier.notify_rejection(
            decision, label,
            reason=reason_message, filter_profile=filter_profile, timeframe=timeframe,
        )

    def report_entry_accepted(
        self,
        plan,
        instrument,
        *,
        quantity: int = 0,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> None:
        label = _short_contract_name(instrument)
        self._notifier.notify_entry_accepted(
            plan, label, quantity=quantity, timeframe=timeframe,
        )


def _short_contract_name(instrument) -> str:
    """Never expose a raw exchange ticker or verbose selector label to users."""
    return getattr(instrument, "short_name", None) or "контракт не указан"
