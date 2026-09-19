from abc import ABC, abstractmethod
from datetime import timedelta

from src.strategies.contracts import Decision, SignalType
from src.trade_management.models import TradePlan


class DecisionFormatter:
    """Переводит решение стратегии в текст уведомления."""

    def __init__(self, tz_offset_hours: float = 0.0) -> None:
        self._tz_offset = tz_offset_hours

    def format(
        self,
        decision: Decision,
        instrument_label: str = "",
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> str:
        parts = self._header_parts(decision, instrument_label, "●", filter_profile, timeframe)

        if filtered_out:
            signal = "❌ Отклонено фильтром."
        elif decision.signal_type is SignalType.BUY:
            signal = f"🟢 ПОКУПКА (BUY) — Цена: {round(decision.price, 3)}"
        elif decision.signal_type is SignalType.SELL:
            signal = f"🔴 ПРОДАЖА (SELL) — Цена: {round(decision.price, 3)}"
        else:
            signal = "⏳ Нет сигнала."

        return " ".join(parts) + f" ➜ {signal}"

    def format_rejection(
        self,
        decision: Decision,
        instrument_label: str = "",
        *,
        reason: str,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> str:
        """Форматирует сообщение о недопуске сделки с маркером ⛔."""
        parts = self._header_parts(decision, instrument_label, "⛔", filter_profile, timeframe)
        return " ".join(parts) + f" ➜ Сделка не допущена: {reason}"

    def _header_parts(
        self,
        decision: Decision,
        instrument_label: str,
        marker: str,
        filter_profile: str,
        timeframe: str,
    ) -> list[str]:
        parts: list[str] = []
        if instrument_label:
            label_block = f"{marker} {instrument_label}"
            if timeframe:
                label_block += f" ({timeframe})"
            parts.append(label_block)
        else:
            parts.append(marker)
        if decision.bar_time is not None:
            parts.append(
                (decision.bar_time + timedelta(hours=self._tz_offset)).strftime("%H:%M")
            )
        if decision.strategy_name:
            strategy_block = f"| {decision.strategy_name}"
            if filter_profile:
                strategy_block += f" [{filter_profile}]"
            parts.append(strategy_block)
        return parts


class TradePlanFormatter:
    """Formats a proposed trade without presenting it as an execution."""

    @staticmethod
    def format(plan: TradePlan, contract_name: str, *, timeframe: str = "") -> str:
        label = f"● {contract_name}"
        if timeframe:
            label += f" ({timeframe})"
        targets = ", ".join(_format_price(target.price) for target in plan.targets) or "нет"
        return (
            f"{label} | {plan.profile.name} ➜ ПЛАН {plan.side} — "
            f"Вход: {_format_price(plan.reference_entry)}, "
            f"Стоп: {_format_price(plan.stop_price)}, Цели: {targets}"
        )


class EntryAcceptedFormatter:
    """Formats an accepted entry: trade is in work, order awaits broker confirmation."""

    @staticmethod
    def format(
        plan: TradePlan,
        contract_name: str,
        *,
        quantity: int = 0,
        timeframe: str = "",
    ) -> str:
        label = f"● {contract_name}"
        if timeframe:
            label += f" ({timeframe})"
        targets = ", ".join(_format_price(target.price) for target in plan.targets) or "нет"
        return (
            f"{label} ➜ Сделка {plan.side}, объём {quantity} — "
            f"Вход: {_format_price(plan.reference_entry)}, "
            f"Стоп: {_format_price(plan.stop_price)}, Цели: {targets} "
            f"— в работе, ждёт подтверждения"
        )


def _format_price(value) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


class DealEventFormatter:
    """Форматирование событий исполнения (order / fill / cancel / clear / over_risk / protective / balance)."""

    @staticmethod
    def format_event(event_type: str, position_id: str, message: str) -> str:
        prefix = {
            "order": "📝 Ордер",
            "fill": "💰 Сделка",
            "cancel": "❌ Отмена",
            "clear": "🏛 Клиринг",
            "over_risk": "⚠️ Over-risk",
            "protective": "🛡 Защита",
            "balance": "💼 Баланс",
        }.get(event_type, "📌 Событие")
        return f"{prefix}: {message}"


class AbstractNotifier(ABC):
    """Доставляет уведомления: форматтер внедряется через конструктор."""

    def __init__(self, formatter: DecisionFormatter | None = None) -> None:
        self._formatter = formatter if formatter is not None else DecisionFormatter()

    def notify_decision(
        self,
        decision: Decision,
        instrument_label: str = "",
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> None:
        self.notify(
            self._formatter.format(
                decision, instrument_label,
                filter_profile=filter_profile, filtered_out=filtered_out,
                timeframe=timeframe,
            )
        )

    def notify_plan(self, plan: TradePlan, contract_name: str, *, timeframe: str = "") -> None:
        self.notify(TradePlanFormatter.format(plan, contract_name, timeframe=timeframe))

    def notify_entry_accepted(
        self,
        plan: TradePlan,
        contract_name: str,
        *,
        quantity: int = 0,
        timeframe: str = "",
    ) -> None:
        self.notify(
            EntryAcceptedFormatter.format(
                plan, contract_name, quantity=quantity, timeframe=timeframe,
            )
        )

    def notify_rejection(
        self,
        decision: Decision,
        instrument_label: str = "",
        *,
        reason: str,
        filter_profile: str = "",
        timeframe: str = "",
    ) -> None:
        self.notify(
            self._formatter.format_rejection(
                decision, instrument_label,
                reason=reason, filter_profile=filter_profile, timeframe=timeframe,
            )
        )

    def notify_event(self, event_type: str, position_id: str, message: str) -> None:
        self.notify(DealEventFormatter.format_event(event_type, position_id, message))

    @abstractmethod
    def notify(self, message: str) -> None: ...
