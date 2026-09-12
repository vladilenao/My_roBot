"""Протокол профиля фильтрации сигналов."""

from __future__ import annotations

from typing import Protocol

from src.market_context.models import MarketContext
from src.strategies.contracts import Decision


class ProfileFilter(Protocol):
    """Конкретный фильтр профиля: чистый трансформер Decision."""

    def apply(self, decision: Decision, ctx: MarketContext) -> Decision: ...
