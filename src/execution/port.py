from __future__ import annotations

from abc import ABC, abstractmethod

from src.logging_setup import get_logger

log = get_logger(__name__)


class ExecutionPort(ABC):
    """Выходной слой принятых решений робота.

    Единая точка, в которую оркестратор отдаёт решение. Реализации: уведомление
    трейдера (`NotifyOnlyExecutionPort`) или имитация исполнения
    (`BrokerExecutionPort`), возвращающая результат сделки.
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


class BrokerExecutionPort(ExecutionPort):
    """Имитированное исполнение: портфельные проверки + брокер через адаптер.

    Обёртка `BrokerExecutionAdapter`, публикующая протокол `ExecutionPort`:
    решение стратегии → сигнал исполнителю → результат сделки.

    При наличии нотификатора решение также доставляется наружу как уведомление
    (`notify_decision`), сохраняя прежние строки о сигналах в командной строке.
    """

    def __init__(self, adapter, notifier=None) -> None:
        self._adapter = adapter
        self._notifier = notifier

    def execute(
        self,
        decision,
        instrument,
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> object | None:
        if self._notifier is not None:
            label = getattr(instrument, "short_name", None) or getattr(instrument, "label", "")
            self._notifier.notify_decision(
                decision, label,
                filter_profile=filter_profile, filtered_out=filtered_out,
                timeframe=timeframe,
            )
        return self._adapter.execute(
            decision,
            instrument,
            filter_profile=filter_profile,
            filtered_out=filtered_out,
            timeframe=timeframe,
        )
