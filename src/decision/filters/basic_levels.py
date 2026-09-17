"""Профиль `basic_levels`: блокировка сигналов против тренда."""

from __future__ import annotations

from dataclasses import replace

from src.market_context.models import MarketContext, TrendDirection
from src.strategies.contracts import Decision, SignalType
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace


class BasicLevelsFilter:
    """Жёсткий фильтр сигналов по направлению тренда.

    Блокирует BUY при нисходящем тренде и SELL при восходящем, превращая их в
    HOLD. Боковой тренд и совпадающие сигналы проходят без изменений.
    """

    def apply(
        self,
        decision: Decision,
        ctx: MarketContext,
        instrument: str = "",
        timeframe: str = "",
    ) -> Decision:
        direction = ctx.trend.direction

        blocked = False
        if decision.signal_type is SignalType.BUY and direction is TrendDirection.DOWN:
            blocked = True
        elif decision.signal_type is SignalType.SELL and direction is TrendDirection.UP:
            blocked = True

        if blocked:
            return replace(decision, signal_type=SignalType.HOLD)
        return decision

    def apply_with_trace(
        self, decision: Decision, ctx: MarketContext, instrument: str = "", timeframe: str = ""
    ) -> tuple[Decision, CalculationTrace]:
        """Apply the entry filter and retain its entirely local decision inputs."""
        result = self.apply(decision, ctx, instrument, timeframe)
        blocked = result.signal_type is SignalType.HOLD and decision.signal_type is not SignalType.HOLD
        return result, calculation_trace(
            "filter.basic_levels", inputs={
                "signal": MeasuredValue(decision.signal_type.value, "signal"),
                "trend": MeasuredValue(ctx.trend.direction.value, "trend-direction"),
                "instrument": MeasuredValue(instrument, "instrument-id"),
                "timeframe": MeasuredValue(timeframe, "timeframe"),
            }, result=MeasuredValue(result.signal_type.value, "signal"),
            reason="trend-opposes-entry" if blocked else "entry-allowed",
            formula="BUY blocked on DOWN; SELL blocked on UP; otherwise pass",
            links=TraceLinks(signal_id=decision.event_id),
            outcome=TraceOutcome.REJECTED if blocked else TraceOutcome.ACCEPTED,
        )
