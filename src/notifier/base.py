from abc import ABC, abstractmethod

from src.strategies.contracts import Decision, SignalType


class DecisionFormatter:
    """Переводит решение стратегии в текст уведомления."""

    def format(self, decision: Decision, instrument_label: str = "") -> str:
        prefix = f"[{instrument_label}] " if instrument_label else ""
        strategy_suffix = f" ({decision.strategy_name})" if decision.strategy_name else ""

        if decision.signal_type is SignalType.BUY:
            msg = f"{prefix}🚀 ПОКУПАТЬ{strategy_suffix}! Цена: {round(decision.price, 3)}"
        elif decision.signal_type is SignalType.SELL:
            msg = f"{prefix}📉 ПРОДАВАТЬ{strategy_suffix}! Цена: {round(decision.price, 3)}"
        else:
            msg = f"{prefix}😴 Отдыхаем{strategy_suffix}, сигналов нет."

        if decision.timeframe:
            msg += f"\nТаймфрейм: {decision.timeframe}"
        if decision.indicator_values:
            indicators = ", ".join(
                f"{k}={v:.2f}" for k, v in decision.indicator_values.items()
            )
            msg += f"\nИндикаторы: {indicators}"

        return msg


class AbstractNotifier(ABC):
    """Доставляет уведомления: форматтер внедряется через конструктор."""

    def __init__(self, formatter: DecisionFormatter | None = None) -> None:
        self._formatter = formatter if formatter is not None else DecisionFormatter()

    def notify_decision(self, decision: Decision, instrument_label: str = "") -> None:
        self.notify(self._formatter.format(decision, instrument_label))

    @abstractmethod
    def notify(self, message: str) -> None: ...
