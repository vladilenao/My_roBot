from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from src.decision.filters import PROFILES, ProfileFilter
from src.market_context.models import MarketContext
from src.strategies.contracts import DEFAULT_FILTER_PROFILE, Decision
from src.logging_setup import get_logger

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
    ) -> Decision:
        profile = self._PROFILES.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self._PROFILES))
            raise ValueError(
                f"Неизвестный профиль фильтрации {profile_name!r}. "
                f"Доступны: {available}"
            )
        return profile.apply(decision, ctx)
