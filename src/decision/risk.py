from __future__ import annotations

from dataclasses import dataclass, replace

from src.market_context.models import MarketContext, SRLevel, SRType
from src.strategies.contracts import Decision, SignalType


@dataclass(frozen=True)
class RiskManager:
    """Расчёт стоп-лосс и тейк-профит на основе уровней S/R.

    SL размещается ниже ближайшего support (BUY) или выше ближайшего
    resistance (SELL); при отсутствии уровня — fallback `default_sl_pct`.
    TP рассчитывается по risk/reward от SL, с привязкой к ближайшему уровню S/R.
    """

    risk_reward_ratio: float = 2.0
    default_sl_pct: float = 0.02
    tp_snap_pct: float = 0.01

    def __post_init__(self) -> None:
        if self.risk_reward_ratio <= 0:
            raise ValueError("risk_reward_ratio должен быть > 0")
        if self.default_sl_pct <= 0:
            raise ValueError("default_sl_pct должен быть > 0")

    def apply(self, decision: Decision, ctx: MarketContext) -> Decision:
        if decision.signal_type is SignalType.HOLD:
            return decision

        price = decision.price
        levels = ctx.sr_levels

        if decision.signal_type is SignalType.BUY:
            return self._apply_buy(decision, price, levels)
        return self._apply_sell(decision, price, levels)

    def _apply_buy(self, decision: Decision, price: float, levels: list[SRLevel]) -> Decision:
        support = self._nearest_below(levels, SRType.SUPPORT, price)
        if support is not None:
            sl = support.price
            sl_label = support.label
        else:
            sl = price * (1 - self.default_sl_pct)
            sl_label = None

        tp_raw = price + (price - sl) * self.risk_reward_ratio
        tp_snap = self._nearest_to_price(levels, tp_raw, price)
        if tp_snap is not None:
            tp = tp_snap.price
            tp_label = tp_snap.label
        else:
            tp = tp_raw
            tp_label = None

        return replace(
            decision,
            stop_loss=round(sl, 4),
            take_profit=round(tp, 4),
            sl_level_label=sl_label,
            tp_level_label=tp_label,
            sl_distance_pct=round((price - sl) / price, 4),
            tp_distance_pct=round((tp - price) / price, 4),
        )

    def _apply_sell(self, decision: Decision, price: float, levels: list[SRLevel]) -> Decision:
        resistance = self._nearest_above(levels, SRType.RESISTANCE, price)
        if resistance is not None:
            sl = resistance.price
            sl_label = resistance.label
        else:
            sl = price * (1 + self.default_sl_pct)
            sl_label = None

        tp_raw = price - (sl - price) * self.risk_reward_ratio
        tp_snap = self._nearest_to_price(levels, tp_raw, price)
        if tp_snap is not None:
            tp = tp_snap.price
            tp_label = tp_snap.label
        else:
            tp = tp_raw
            tp_label = None

        return replace(
            decision,
            stop_loss=round(sl, 4),
            take_profit=round(tp, 4),
            sl_level_label=sl_label,
            tp_level_label=tp_label,
            sl_distance_pct=round((sl - price) / price, 4),
            tp_distance_pct=round((price - tp) / price, 4),
        )

    @staticmethod
    def _nearest_below(levels: list[SRLevel], sr_type: SRType, price: float) -> SRLevel | None:
        candidates = [lev for lev in levels if lev.sr_type is sr_type and lev.price < price]
        if not candidates:
            return None
        return min(candidates, key=lambda lev: abs(lev.price - price))

    @staticmethod
    def _nearest_above(levels: list[SRLevel], sr_type: SRType, price: float) -> SRLevel | None:
        candidates = [lev for lev in levels if lev.sr_type is sr_type and lev.price > price]
        if not candidates:
            return None
        return min(candidates, key=lambda lev: abs(lev.price - price))

    def _nearest_to_price(self, levels: list[SRLevel], raw_target: float, current_price: float) -> SRLevel | None:
        candidates = [
            lev for lev in levels if abs(lev.price - raw_target) / current_price < self.tp_snap_pct
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda lev: abs(lev.price - raw_target))
