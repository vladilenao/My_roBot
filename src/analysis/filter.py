from __future__ import annotations

from dataclasses import dataclass, replace

from src.analysis.models import MarketContext, TrendDirection
from src.strategies.contracts import Decision, SignalType


@dataclass(frozen=True)
class SignalFilter:
    """Жёсткий фильтр сигналов по направлению тренда.

    Блокирует BUY при нисходящем тренде и SELL при восходящем, превращая их в
    HOLD. Боковой тренд и совпадающие сигналы проходят без изменений. Всегда
    обогащает Decision полями `trend_direction` и `trend_confidence`.
    """

    def apply(self, decision: Decision, ctx: MarketContext) -> Decision:
        direction = ctx.trend.direction
        direction_str = direction.value

        blocked = False
        if decision.signal_type is SignalType.BUY and direction is TrendDirection.DOWN:
            blocked = True
        elif decision.signal_type is SignalType.SELL and direction is TrendDirection.UP:
            blocked = True

        confidence = 0.0 if blocked or decision.signal_type is SignalType.HOLD else ctx.trend.strength

        if blocked:
            decision = replace(decision, signal_type=SignalType.HOLD)

        return replace(
            decision,
            trend_direction=direction_str,
            trend_confidence=confidence,
        )
