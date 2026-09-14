"""Протокол профиля фильтрации сигналов."""

from __future__ import annotations

from typing import Protocol

from src.market_context.models import MarketContext
from src.strategies.contracts import Decision


class ProfileFilter(Protocol):
    """Конкретный фильтр профиля: чистый трансформер Decision.

    `instrument`/`timeframe` — контекст привязки для данных-зависимых профилей
    (``triple_screen``: выбор старших ТФ). Профили без потребности в данных
    (``raw``, ``basic_levels``) игнорируют эти параметры.
    """

    def apply(
        self,
        decision: Decision,
        ctx: MarketContext,
        instrument: str = "",
        timeframe: str = "",
    ) -> Decision: ...
