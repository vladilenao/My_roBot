from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from src.decision.filters import PROFILES, ProfileFilter
from src.market_context.models import MarketContext
from src.strategies.contracts import DEFAULT_FILTER_PROFILE, Decision, SignalType
from src.logging_setup import get_logger
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace

log = get_logger(__name__)


@dataclass(frozen=True)
class FilterVerdict:
    """Решение фильтра по входному кандидату: сигнал, трасса и причина отказа.

    ``reason`` — человеческая причина отказа для уведомления о недопуске;
    пуста, если сигнал пропущен.
    """

    decision: Decision
    trace: CalculationTrace | None
    reason: str = ""

    @property
    def rejected(self) -> bool:
        return bool(self.reason)


@dataclass(frozen=True)
class SignalFilter:
    """Фасад фильтрации сигналов: фабричный выбор профиля по имени.

    Инкапсулирует реестр профилей (`decision.filters.PROFILES`): оркестратор
    передаёт имя профиля привязки, конкретный фильтр конструируется внутри.
    Профиль по умолчанию — `basic_levels`. Новые профили добавляются
    регистрацией в реестре без изменения главного цикла робота.
    """

    _PROFILES: ClassVar[dict[str, ProfileFilter]] = PROFILES

    def apply(
        self,
        decision: Decision,
        ctx: MarketContext,
        profile_name: str = DEFAULT_FILTER_PROFILE,
        instrument: str = "",
        timeframe: str = "",
    ) -> Decision:
        profile = self._PROFILES.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self._PROFILES))
            raise ValueError(
                f"Неизвестный профиль фильтрации {profile_name!r}. "
                f"Доступны: {available}"
            )
        return profile.apply(decision, ctx, instrument=instrument, timeframe=timeframe)

    def apply_with_trace(
        self,
        decision: Decision,
        ctx: MarketContext,
        profile_name: str = DEFAULT_FILTER_PROFILE,
        instrument: str = "",
        timeframe: str = "",
    ):
        profile = self._PROFILES.get(profile_name)
        if profile is None:
            return self.apply(decision, ctx, profile_name, instrument, timeframe), None
        traced = getattr(profile, "apply_with_trace", None)
        if callable(traced):
            return traced(decision, ctx, instrument, timeframe)
        result = profile.apply(decision, ctx, instrument=instrument, timeframe=timeframe)
        blocked = result.signal_type is SignalType.HOLD and decision.signal_type is not SignalType.HOLD
        return result, calculation_trace(
            f"filter.{profile_name}",
            inputs={
                "signal": MeasuredValue(decision.signal_type.value, "signal"),
                "instrument": MeasuredValue(instrument, "instrument-id"),
                "timeframe": MeasuredValue(timeframe, "timeframe"),
            },
            result=MeasuredValue(result.signal_type.value, "signal"),
            reason="filter-rejected" if blocked else "filter-allowed",
            formula="signal and market context -> filtered signal",
            links=TraceLinks(signal_id=decision.event_id),
            outcome=TraceOutcome.REJECTED if blocked else TraceOutcome.ACCEPTED,
        )

    def judge(
        self,
        decision: Decision,
        ctx: MarketContext,
        profile_name: str = DEFAULT_FILTER_PROFILE,
        instrument: str = "",
        timeframe: str = "",
    ) -> FilterVerdict:
        """Применить профиль к входному кандидату и объяснить отказ.

        Причину даёт ``rejection_text(trace)`` профиля — из входов трассы,
        чтобы аудит и уведомление не расходились.
        """
        filtered, trace = self.apply_with_trace(decision, ctx, profile_name, instrument, timeframe)
        if decision.signal_type is SignalType.HOLD or filtered.signal_type is not SignalType.HOLD:
            return FilterVerdict(filtered, trace)
        explain = getattr(self._PROFILES.get(profile_name), "rejection_text", None)
        reason = explain(trace) if callable(explain) and trace is not None else ""
        return FilterVerdict(filtered, trace, reason or f"сигнал отклонён фильтром {profile_name}")
