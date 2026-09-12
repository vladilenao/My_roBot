"""Профиль `raw`: Null Object — пропускает решение без изменений."""

from __future__ import annotations

from src.market_context.models import MarketContext
from src.strategies.contracts import Decision


class NullFilter:
    """Отсутствие фильтрации: сигнал и поля тренда не трогаются."""

    def apply(self, decision: Decision, ctx: MarketContext) -> Decision:
        return decision
