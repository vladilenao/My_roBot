"""ATR trend trade-management profile."""

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
    shared_planning_rules,
)
from src.trade_management.profiles.rules import (
    add_quantity,
    allocate_target_quantities,
    initial_stop,
    initial_target,
    validate_price,
    validate_price_step,
    validate_stop,
)


class AtrTrendProfile(TradeManagementProfile):
    """Plan an ATR stop, take TP1, then trail the remaining position."""

    NAME = "atr_trend"

    @shared_planning_rules
    def plan(self, context: PlanningContext) -> TradePlan | ProfileResult:
        side = _side(context.signal.signal_type)
        if side is None:
            return ProfileResult(state={"reason": "insufficient-history"})

        atr = context.market.get("atr")
        if atr is None:
            return ProfileResult(state={"reason": "insufficient-history"})
        entry = validate_price(context.signal.price, "entry")
        atr_value = validate_price(atr, "atr")
        step = validate_price_step(_market_value(context.market, "price_step"))
        initial_k = _positive_parameter(context.profile.parameters, "initial_k", "2")
        if context.market.get("algorithm_version") == "economics-v2":
            multiples = context.profile.parameters.get("target_R", (1, 2))
            shares = context.profile.parameters.get("shares", ("0.25", "0.25"))
        else:
            multiples = (_positive_parameter(context.profile.parameters, "tp1_R", "1"),)
            shares = (_share(context.profile.parameters.get("tp1_share", "0.5")),)
        raw_stop = entry - initial_k * atr_value if side == "BUY" else entry + initial_k * atr_value
        stop = initial_stop(raw_stop, step, side)
        entry, stop = validate_stop(entry, stop, side)
        risk = entry - stop if side == "BUY" else stop - entry
        try:
            targets = tuple(TargetPlan(f"tp-{index}", entry if context.market.get("algorithm_version") == "economics-v2" else initial_target(
                entry + risk * Decimal(str(multiple)) if side == "BUY" else entry - risk * Decimal(str(multiple)),
                step, side), Decimal(str(share)))
                for index, (multiple, share) in enumerate(zip(multiples, shares, strict=True), start=1))
        except ValueError:
            return ProfileResult(state={"reason": "target-not-ahead"})
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
        state = context.state
        if state.quantity <= 0 or state.average_price is None:
            return ProfileResult()
        if _trailing_is_active(context):
            return self._trail(context)
        return self._pyramid(context)

    def _trail(self, context: ManagementContext) -> ProfileResult:
        atr = context.market.get("atr")
        extreme_key = "high" if context.plan.side == "BUY" else "low"
        extreme_value = context.market.get(extreme_key)
        if atr is None or extreme_value is None:
            if context.plan.algorithm_version == "economics-v2":
                return ProfileResult(state={"reason": "insufficient-history", "trailing_active": True, "adds_disabled": True})
            return ProfileResult(state={"reason": "insufficient-history"})

        atr_value = validate_price(atr, "atr")
        observed = validate_price(extreme_value, extreme_key)
        previous_extreme = context.state.trailing_extreme
        extreme = (
            max(previous_extreme, observed)
            if context.plan.side == "BUY" and previous_extreme is not None
            else min(previous_extreme, observed)
            if previous_extreme is not None
            else observed
        )
        step = validate_price_step(_market_value(context.market, "price_step"))
        trail_k = _positive_parameter(context.plan.profile.parameters, "trail_k", "2")
        candidate = initial_stop(
            extreme - trail_k * atr_value
            if context.plan.side == "BUY"
            else extreme + trail_k * atr_value,
            step,
            context.plan.side,
        )
        current_stop = context.state.confirmed_stop or context.plan.stop_price
        improves = candidate > current_stop if context.plan.side == "BUY" else candidate < current_stop
        profile_state: dict[str, object] = {
            "trailing_active": True,
            "trailing_extreme": extreme,
            "adds_disabled": True,
        }
        if not improves:
            return ProfileResult(state=profile_state)
        return ProfileResult(
            actions=(
                MoveStop(
                    command_id=f"{context.plan.trade_id}:atr-trail:{context.state.state_revision}",
                    trade_id=context.plan.trade_id,
                    state_revision=context.state.state_revision,
                    reason="atr-trailing-stop",
                    stop_price=candidate,
                ),
            ),
            state=profile_state,
        )

    def _pyramid(self, context: ManagementContext) -> ProfileResult:
        state = context.state
        market = context.market
        candidate = market.get("add_signal_price")
        if candidate is None:
            return ProfileResult()
        price = validate_price(candidate, "add_signal_price")
        last_entry = validate_price(market.get("last_entry_price", state.average_price), "last_entry_price")
        risk = (
            context.plan.reference_entry - context.plan.stop_price
            if context.plan.side == "BUY"
            else context.plan.stop_price - context.plan.reference_entry
        )
        if context.plan.algorithm_version == "economics-v2" and state.initial_stop_distance is not None:
            risk = state.initial_stop_distance
        advance_r = _positive_parameter(context.plan.profile.parameters, "advance_R", "0.5")
        advanced = (
            price - last_entry >= risk * advance_r
            if context.plan.side == "BUY"
            else last_entry - price >= risk * advance_r
        )
        quantity = add_quantity(
            state,
            context.plan.profile.parameters,
            price,
            context.plan.side,
            pending_increase=bool(market.get("pending_increase")),
        )
        if not advanced or quantity is None:
            return ProfileResult()
        return ProfileResult(
            actions=(
                AddToTrade(
                    command_id=f"{context.plan.trade_id}:atr-add:{context.state.state_revision}",
                    trade_id=context.plan.trade_id,
                    state_revision=context.state.state_revision,
                    reason="atr-profitable-advance",
                    quantity=quantity,
                ),
            )
        )


def _trailing_is_active(context: ManagementContext) -> bool:
    if context.plan.algorithm_version == "economics-v2":
        if context.state.trailing_active or context.state.completed_target_ids:
            return True
        if not context.market.get("holding_bar_eligible", False):
            return False
        maximum = context.state.max_quantity or context.state.quantity
        allocation = allocate_target_quantities(maximum, context.plan.targets, retain_remainder_for_trailing=True)
        if any(a.target_id == context.plan.targets[0].target_id for a in allocation.targets):
            return False
        threshold = context.state.target_prices.get(context.plan.targets[0].target_id, context.plan.targets[0].price)
        observed = context.market.get("high" if context.plan.side == "BUY" else "low")
        return observed is not None and (validate_price(observed) >= threshold if context.plan.side == "BUY" else validate_price(observed) <= threshold)
    if "tp-1" in context.state.completed_target_ids:
        return True
    # A one-contract position has no TP1 order; its threshold touch enables trailing.
    if context.state.quantity != 1:
        return False
    threshold = context.plan.targets[0].price
    if context.plan.side == "BUY":
        high = context.market.get("high")
        return high is not None and validate_price(high, "high") >= threshold
    low = context.market.get("low")
    return low is not None and validate_price(low, "low") <= threshold


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


def _positive_parameter(parameters: Mapping[str, object], name: str, default: str) -> Decimal:
    value = validate_price(parameters.get(name, default), name)
    return value


def _share(value: object) -> Decimal:
    share = Decimal(str(value))
    if not Decimal("0") < share <= Decimal("1"):
        raise ValueError("share must be in (0, 1]")
    return share


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _created_at(context: PlanningContext) -> datetime:
    created_at = context.market.get("created_at") or context.signal.bar_time
    if created_at is None:
        return datetime.min
    if isinstance(created_at, datetime):
        return created_at
    raise ValueError("created_at must be a datetime")
