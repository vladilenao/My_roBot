from abc import ABC, abstractmethod

from src.strategies.contracts import Decision, SignalType


class DecisionFormatter:
    """Переводит решение стратегии в текст уведомления."""

    def format(self, decision: Decision, instrument_label: str = "") -> str:
        parts: list[str] = []
        if instrument_label:
            parts.append(f"● {instrument_label}")
        else:
            parts.append("●")
        if decision.bar_time is not None:
            parts.append(decision.bar_time.strftime("%H:%M"))
        if decision.strategy_name:
            parts.append(f"| {decision.strategy_name}")

        if decision.signal_type is SignalType.BUY:
            signal = f"🟢 ПОКУПКА (BUY) — Цена: {round(decision.price, 3)}"
        elif decision.signal_type is SignalType.SELL:
            signal = f"🔴 ПРОДАЖА (SELL) — Цена: {round(decision.price, 3)}"
        else:
            signal = "⏳ Нет сигнала."

        return " ".join(parts) + f" ➜ {signal}"


class AbstractNotifier(ABC):
    """Доставляет уведомления: форматтер внедряется через конструктор."""

    def __init__(self, formatter: DecisionFormatter | None = None) -> None:
        self._formatter = formatter if formatter is not None else DecisionFormatter()

    def notify_decision(self, decision: Decision, instrument_label: str = "") -> None:
        self.notify(self._formatter.format(decision, instrument_label))

    @abstractmethod
    def notify(self, message: str) -> None: ...
