from src.strategies.base import Decision, SignalType


def format_decision(decision: Decision, instrument_label: str = "") -> str:
    """Переводит решение стратегии в текст уведомления."""

    prefix = f"[{instrument_label}] " if instrument_label else ""

    if decision.signal_type is SignalType.BUY:
        return f"{prefix}🚀 ПОКУПАТЬ! Цена: {round(decision.price, 3)}"
    if decision.signal_type is SignalType.SELL:
        return f"{prefix}📉 ПРОДАВАТЬ! Цена: {round(decision.price, 3)}"
    return f"{prefix}😴 Отдыхаем, сигналов нет."
