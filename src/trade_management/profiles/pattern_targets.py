"""AB=CD target trade-management profile."""

from datetime import datetime
from decimal import Decimal
from typing import Mapping

import pandas as pd

from src.strategies.contracts import SignalType
from src.trade_management.actions import AddToTrade, MoveStop
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan
from src.trade_management.profiles.base import (
    ManagementContext,
    PlanningContext,
    ProfileResult,
    TradeManagementProfile,
    shared_management_rules,
)
from src.trade_management.profiles.rules import (
    add_quantity,
    cost_aware_break_even,
    initial_stop,
    initial_target,
    validate_price,
    validate_price_step,
    validate_stop,
)


class PatternTargetsProfile(TradeManagementProfile):
    """Plan AB=CD invalidation, midpoint and D targets from confirmed context."""

    NAME = "pattern_targets"
    REQUIRED_STRATEGY_CAPABILITIES = frozenset({"pattern_context"})

    def plan(self, context: PlanningContext) -> TradePlan | ProfileResult:
        side = _side(context.signal.signal_type)
        if side is None:
            return ProfileResult(state={"reason": "missing-pattern-context"})
        pattern = _confirmed_pattern(context)
        if isinstance(pattern, ProfileResult):
            return pattern
        pattern_id, c, d = pattern
        entry = validate_price(context.signal.price, "entry")
        step = validate_price_step(_market_value(context.market, "price_step"))
        buffer_ticks = _non_negative_int(
            context.profile.parameters.get("buffer_ticks", 1), "buffer_ticks"
        )
        if (side == "BUY" and d <= entry) or (side == "SELL" and d >= entry):
            return ProfileResult(state={"reason": "target-not-ahead"})
        stop = initial_stop(
            c - step * buffer_ticks if side == "BUY" else c + step * buffer_ticks,
            step,
            side,
        )
        try:
            entry, stop = validate_stop(entry, stop, side)
        except ValueError:
            return ProfileResult(state={"reason": "invalid-pattern-context"})
        midpoint = initial_target((entry + d) / 2, step, side)
        target_d = initial_target(d, step, side)
        if (side == "BUY" and (midpoint <= entry or target_d <= midpoint)) or (
            side == "SELL" and (midpoint >= entry or target_d >= midpoint)
        ):
            return ProfileResult(state={"reason": "target-not-ahead"})
        shares = _shares(context.profile.parameters.get("shares", ("0.5", "0.5")))
        # The formation identity belongs to the immutable plan so later add
        # candidates cannot silently switch to another AB=CD pattern.
        snapshot = ProfileSnapshot(
            context.profile.name,
            context.profile.version,
            {**context.profile.parameters, "_pattern_id": pattern_id},
        )
        return TradePlan(
            trade_id=context.trade_id,
            assignment_id=context.assignment_id,
            instrument_id=context.instrument_id,
            side=side,
            signal_id=context.signal.event_id or context.trade_id,
            reference_entry=entry,
            stop_price=stop,
            targets=(TargetPlan("tp-1", midpoint, shares[0]), TargetPlan("tp-2", target_d, shares[1])),
            profile=snapshot,
            created_at=_created_at(context),
        )

    @shared_management_rules
    def manage(self, context: ManagementContext) -> ProfileResult:
        if context.state.quantity <= 0 or context.state.average_price is None:
            return ProfileResult()
        actions = self._break_even(context)
        add = self._pattern_add(context)
        return ProfileResult(actions=actions + add)

    def _break_even(self, context: ManagementContext) -> tuple[MoveStop, ...]:
        if "tp-1" not in context.state.completed_target_ids:
            return ()
        current_stop = context.state.confirmed_stop or context.plan.stop_price
        break_even = cost_aware_break_even(
            context.state.average_price,
            context.plan.side,
            _market_value(context.market, "price_step"),
            _market_value(context.market, "step_cost"),
            context.state.quantity,
            entry_cost=context.market.get("entry_cost", Decimal("0")),
            exit_cost=context.market.get("exit_cost", Decimal("0")),
            slippage_cost=context.market.get("slippage_cost", Decimal("0")),
        )
        improves = break_even > current_stop if context.plan.side == "BUY" else break_even < current_stop
        if not improves:
            return ()
        return (
            MoveStop(
                f"{context.plan.trade_id}:break-even:{context.state.state_revision}",
                context.plan.trade_id,
                context.state.state_revision,
                "cost-aware-break-even",
                break_even,
            ),
        )

    def _pattern_add(self, context: ManagementContext) -> tuple[AddToTrade, ...]:
        state = context.state
        pattern_id = context.market.get("add_pattern_id", context.market.get("pattern_id"))
        if pattern_id != context.plan.profile.parameters.get("_pattern_id"):
            return ()
        candidate = context.market.get("add_signal_price")
        if candidate is None:
            return ()
        price = validate_price(candidate, "add_signal_price")
        quantity = add_quantity(
            state,
            context.plan.profile.parameters,
            price,
            context.plan.side,
            pending_increase=bool(context.market.get("pending_increase")),
        )
        if quantity is None:
            return ()
        return (
            AddToTrade(
                f"{context.plan.trade_id}:pattern-add:{state.state_revision}",
                context.plan.trade_id,
                state.state_revision,
                "pattern-profitable-same-formation",
                quantity,
            ),
        )


def _confirmed_pattern(context: PlanningContext) -> tuple[str, Decimal, Decimal] | ProfileResult:
    references = context.signal.idea_references
    if not references:
        return ProfileResult(state={"reason": "missing-pattern-context"})
    try:
        pattern_id = references["pattern_id"]
        c = validate_price(references["c"], "pattern_c")
        d = validate_price(references["d"], "pattern_d")
        available_at = pd.Timestamp(references["time_available"])
        signal_available_at = pd.Timestamp(context.signal.available_at)
    except (KeyError, TypeError, ValueError):
        return ProfileResult(state={"reason": "missing-pattern-context"})
    if not isinstance(pattern_id, str) or not pattern_id:
        return ProfileResult(state={"reason": "missing-pattern-context"})
    if pd.isna(available_at) or pd.isna(signal_available_at) or signal_available_at < available_at:
        return ProfileResult(state={"reason": "pattern-unavailable"})
    return pattern_id, c, d


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


def _share(value: object) -> Decimal:
    share = Decimal(str(value))
    if not Decimal("0") < share <= Decimal("1"):
        raise ValueError("add_fraction must be in (0, 1]")
    return share


def _shares(values: object) -> tuple[Decimal, Decimal]:
    if not isinstance(values, (tuple, list)) or len(values) != 2:
        raise ValueError("shares must contain two values")
    result = tuple(_share(value) for value in values)
    if sum(result) > 1:
        raise ValueError("shares must sum to at most 1")
    return result[0], result[1]


def _created_at(context: PlanningContext) -> datetime:
    created_at = context.market.get("created_at") or context.signal.bar_time
    if created_at is None:
        return datetime.min
    if isinstance(created_at, datetime):
        return created_at
    raise ValueError("created_at must be a datetime")
