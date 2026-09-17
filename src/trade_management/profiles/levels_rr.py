"""Support/resistance risk-reward trade-management profile."""

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from src.strategies.contracts import SignalType
from src.trade_management.actions import AddToTrade, MoveStop
from src.trade_management.models import TargetPlan, TradePlan
from src.trade_management.profiles.base import (
    ManagementContext,
    PlanningContext,
    ProfileResult,
    TradeManagementProfile,
    shared_management_rules,
)
from src.trade_management.profiles.rules import (
    cost_aware_break_even,
    add_quantity,
    initial_stop,
    initial_target,
    validate_price,
    validate_price_step,
    validate_stop,
)


class LevelsRrProfile(TradeManagementProfile):
    """Plan from correctly sided S/R and move protection after the first target."""

    NAME = "levels_rr"

    def plan(self, context: PlanningContext) -> TradePlan | ProfileResult:
        side = _side(context.signal.signal_type)
        if side is None:
            return ProfileResult(state={"reason": "missing-structure"})

        entry = validate_price(context.signal.price, "entry")
        step = validate_price_step(_market_value(context.market, "price_step"))
        buffer_ticks = _non_negative_int(
            context.profile.parameters.get("buffer_ticks", 1), "buffer_ticks"
        )
        structure_key = "support" if side == "BUY" else "resistance"
        structure = context.market.get(structure_key)
        if structure is None:
            return ProfileResult(state={"reason": "missing-structure"})

        level = validate_price(structure, structure_key)
        stop = initial_stop(
            level - step * buffer_ticks if side == "BUY" else level + step * buffer_ticks,
            step,
            side,
        )
        try:
            entry, stop = validate_stop(entry, stop, side)
        except ValueError:
            return ProfileResult(state={"reason": "missing-structure"})

        target_rs = _positive_ascending(
            context.profile.parameters.get("target_R", (1, 2)), "target_R"
        )
        shares = _shares(context.profile.parameters.get("shares", ("0.5", "0.5")), len(target_rs))
        risk = entry - stop if side == "BUY" else stop - entry
        targets = tuple(
            TargetPlan(
                f"tp-{index}",
                initial_target(
                    entry + risk * multiple if side == "BUY" else entry - risk * multiple,
                    step,
                    side,
                ),
                share,
            )
            for index, (multiple, share) in enumerate(zip(target_rs, shares), start=1)
        )
        return TradePlan(
            trade_id=context.trade_id,
            assignment_id=context.assignment_id,
            instrument_id=context.instrument_id,
            side=side,
            signal_id=context.signal.event_id or context.trade_id,
            reference_entry=entry,
            stop_price=stop,
            targets=targets,
            profile=context.profile,
            created_at=_created_at(context),
        )

    @shared_management_rules
    def manage(self, context: ManagementContext) -> ProfileResult:
        add = self._add(context)
        if "tp-1" not in context.state.completed_target_ids:
            return ProfileResult(actions=add)

        current_stop = context.state.confirmed_stop or context.plan.stop_price
        quantity = context.state.quantity
        average_price = context.state.average_price
        if quantity <= 0 or average_price is None:
            return ProfileResult()

        break_even = cost_aware_break_even(
            average_price,
            context.plan.side,
            _market_value(context.market, "price_step"),
            _market_value(context.market, "step_cost"),
            quantity,
            entry_cost=context.market.get("entry_cost", Decimal("0")),
            exit_cost=context.market.get("exit_cost", Decimal("0")),
            slippage_cost=context.market.get("slippage_cost", Decimal("0")),
        )
        improves = (
            break_even > current_stop
            if context.plan.side == "BUY"
            else break_even < current_stop
        )
        if not improves:
            return ProfileResult(actions=add)
        return ProfileResult(
            actions=(
                MoveStop(
                    command_id=f"{context.plan.trade_id}:break-even:{context.state.state_revision}",
                    trade_id=context.plan.trade_id,
                    state_revision=context.state.state_revision,
                    reason="cost-aware-break-even",
                    stop_price=break_even,
                ),
            ) + add
        )

    @staticmethod
    def _add(context: ManagementContext) -> tuple[AddToTrade, ...]:
        """Allow the explicitly configured levels profile to pyramid profitably."""
        candidate = context.market.get("add_signal_price")
        if candidate is None:
            return ()
        quantity = add_quantity(
            context.state,
            context.plan.profile.parameters,
            validate_price(candidate, "add_signal_price"),
            context.plan.side,
            pending_increase=bool(context.market.get("pending_increase")),
        )
        if quantity is None:
            return ()
        return (
            AddToTrade(
                f"{context.plan.trade_id}:levels-add:{context.state.state_revision}",
                context.plan.trade_id,
                context.state.state_revision,
                "levels-profitable-add",
                quantity,
            ),
        )


def _side(signal_type: SignalType) -> str | None:
    if signal_type is SignalType.BUY:
        return "BUY"
    if signal_type is SignalType.SELL:
        return "SELL"
    return None


def _market_value(market: Mapping[str, object], name: str) -> object:
    try:
        return market[name]
    except KeyError as exc:
        raise ValueError(f"market requires {name}") from exc


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_ascending(values: object, name: str) -> tuple[Decimal, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError(f"{name} must be a non-empty sequence")
    result = tuple(validate_price(value, name) for value in values)
    if any(right <= left for left, right in zip(result, result[1:])):
        raise ValueError(f"{name} must be strictly ascending")
    return result


def _shares(values: object, count: int) -> tuple[Decimal, ...]:
    if not isinstance(values, (tuple, list)) or len(values) != count:
        raise ValueError("shares must match target_R")
    result = tuple(Decimal(str(value)) for value in values)
    if any(share <= 0 or share > 1 for share in result) or sum(result) > 1:
        raise ValueError("shares must be in (0, 1] and sum to at most 1")
    return result


def _created_at(context: PlanningContext) -> datetime:
    created_at = context.market.get("created_at") or context.signal.bar_time
    if created_at is None:
        return datetime.min
    if isinstance(created_at, datetime):
        return created_at
    raise ValueError("created_at must be a datetime")
