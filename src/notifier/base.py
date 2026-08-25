from abc import ABC, abstractmethod

from src.strategies.contracts import Decision, SignalType


class DecisionFormatter:
    """Переводит решение стратегии в текст уведомления."""

    def format(self, decision: Decision, instrument_label: str = "") -> str:
        prefix = f"[{instrument_label}] " if instrument_label else ""

        if decision.signal_type is SignalType.BUY:
            return f"{prefix}🚀 ПОКУПАТЬ! Цена: {round(decision.price, 3)}"
        if decision.signal_type is SignalType.SELL:
            return f"{prefix}📉 ПРОДАВАТЬ! Цена: {round(decision.price, 3)}"
        return f"{prefix}😴 Отдыхаем, сигналов нет."


class AbstractNotifier(ABC):
    """Доставляет уведомления: форматтер внедряется через конструктор."""

    def __init__(self, formatter: DecisionFormatter | None = None) -> None:
        self._formatter = formatter if formatter is not None else DecisionFormatter()

    def notify_decision(self, decision: Decision, instrument_label: str = "") -> None:
        self.notify(self._formatter.format(decision, instrument_label))

    @abstractmethod
    def notify(self, message: str) -> None: ...
