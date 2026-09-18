from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from src.decision.filters import PROFILES, ProfileFilter
from src.market_context.models import MarketContext
from src.strategies.contracts import DEFAULT_FILTER_PROFILE, Decision, SignalType
from src.logging_setup import get_logger
from src.trade_management.audit import MeasuredValue, TraceLinks, TraceOutcome, calculation_trace

log = get_logger(__name__)


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
        return result, calculation_trace(
            f"filter.{profile_name}",
            inputs={
                "signal": MeasuredValue(decision.signal_type.value, "signal"),
                "instrument": MeasuredValue(instrument, "instrument-id"),
                "timeframe": MeasuredValue(timeframe, "timeframe"),
            },
            result=MeasuredValue(result.signal_type.value, "signal"),
            reason="filter-rejected" if result.signal_type is SignalType.HOLD else "filter-allowed",
            formula="signal and market context -> filtered signal",
            links=TraceLinks(signal_id=decision.event_id),
            outcome=TraceOutcome.ACCEPTED,
        )
