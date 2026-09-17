"""Moving-average cloud trade-management profile."""

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from src.strategies.contracts import SignalType
from src.trade_management.actions import AddToTrade, CloseTrade, MoveStop, ReduceTrade
from src.trade_management.models import TradePlan
from src.trade_management.profiles.base import (
    ManagementContext,
    PlanningContext,
    ProfileResult,
    TradeManagementProfile,
    shared_management_rules,
)
from src.trade_management.profiles.rules import (
    add_quantity,
    initial_stop,
    validate_price,
    validate_price_step,
    validate_stop,
)


class MaCloudProfile(TradeManagementProfile):
    """Protect actual positions with an MA cloud and manage signal exits."""

    NAME = "ma_cloud"

    def plan(self, context: PlanningContext) -> TradePlan | ProfileResult:
        side = _side(context.signal.signal_type)
        if side is None:
            return ProfileResult(state={"reason": "insufficient-history"})
        try:
            entry = validate_price(context.signal.price, "entry")
            ma10, ma40 = _moving_averages(context.market)
            step = validate_price_step(_market_value(context.market, "price_step"))
            buffer_ticks = _non_negative_int(
                context.profile.parameters.get("buffer_ticks", 1), "buffer_ticks"
            )
            boundary = min(ma10, ma40) if side == "BUY" else max(ma10, ma40)
            stop = initial_stop(
                boundary - step * buffer_ticks
                if side == "BUY"
                else boundary + step * buffer_ticks,
                step,
                side,
            )
            entry, stop = validate_stop(entry, stop, side)
        except (KeyError, ValueError):
            return ProfileResult(state={"reason": "insufficient-history"})
        return TradePlan(
            trade_id=context.trade_id,
            assignment_id=context.assignment_id,
            instrument_id=context.instrument_id,
            side=side,
            signal_id=context.signal.event_id or context.trade_id,
            reference_entry=entry,
            stop_price=stop,
            targets=(),
            profile=context.profile,
            created_at=_created_at(context),
        )

    @shared_management_rules
    def manage(self, context: ManagementContext) -> ProfileResult:
        state = context.state
        if state.quantity <= 0 or state.average_price is None:
            return ProfileResult()
        ma10, ma40 = _moving_averages(context.market)
        close = validate_price(_market_value(context.market, "close"), "close")
        bar_id = str(context.market.get("bar_id", state.state_revision))

        if _beyond_ma40(context.plan.side, close, ma40):
            return ProfileResult(
                actions=(
                    CloseTrade(
                        f"{context.plan.trade_id}:ma40-exit:{bar_id}",
                        context.plan.trade_id,
                        state.state_revision,
                        "ma40-opposite-close",
                    ),
                )
            )

        actions = _improving_stop(context, ma10, ma40, bar_id)
        if state.quantity > 1 and _partial_exit(context.plan.side, close, ma10, ma40):
            actions += (
                ReduceTrade(
                    f"{context.plan.trade_id}:ma-partial-exit:{bar_id}",
                    context.plan.trade_id,
                    state.state_revision,
                    "ma10-or-cloud-partial-exit",
                    1,
                ),
            )
            return ProfileResult(actions=actions)
        add = _profitable_retest_add(context, close, bar_id)
        return ProfileResult(actions=actions + add.actions, state=add.state)


def _improving_stop(
    context: ManagementContext, ma10: Decimal, ma40: Decimal, bar_id: str
) -> tuple[MoveStop, ...]:
    step = validate_price_step(_market_value(context.market, "price_step"))
    buffer_ticks = _non_negative_int(
        context.plan.profile.parameters.get("buffer_ticks", 1), "buffer_ticks"
    )
    boundary = min(ma10, ma40) if context.plan.side == "BUY" else max(ma10, ma40)
    candidate = initial_stop(
        boundary - step * buffer_ticks
        if context.plan.side == "BUY"
        else boundary + step * buffer_ticks,
        step,
        context.plan.side,
    )
    current = context.state.confirmed_stop or context.plan.stop_price
    improves = candidate > current if context.plan.side == "BUY" else candidate < current
    if not improves:
        return ()
    return (
        MoveStop(
            f"{context.plan.trade_id}:ma-cloud-stop:{bar_id}",
            context.plan.trade_id,
            context.state.state_revision,
            "ma-cloud-protective-stop",
            candidate,
        ),
    )


def _profitable_retest_add(
    context: ManagementContext, close: Decimal, bar_id: str
) -> ProfileResult:
    state = context.state
    if not context.market.get("cloud_retest"):
        return ProfileResult()
    candidate = context.market.get("add_signal_price", close)
    price = validate_price(candidate, "add_signal_price")
    quantity = add_quantity(
        state,
        context.plan.profile.parameters,
        price,
        context.plan.side,
        pending_increase=bool(context.market.get("pending_increase")),
    )
    if quantity is None:
        return ProfileResult()
    return ProfileResult(
        actions=(
            AddToTrade(
                f"{context.plan.trade_id}:ma-cloud-add:{bar_id}",
                context.plan.trade_id,
                state.state_revision,
                "ma-cloud-profitable-retest",
                quantity,
            ),
        )
    )


def _moving_averages(market: Mapping[str, object]) -> tuple[Decimal, Decimal]:
    return (
        validate_price(_market_value(market, "ma10"), "ma10"),
        validate_price(_market_value(market, "ma40"), "ma40"),
    )


def _beyond_ma40(side: str, close: Decimal, ma40: Decimal) -> bool:
    return close < ma40 if side == "BUY" else close > ma40


def _partial_exit(side: str, close: Decimal, ma10: Decimal, ma40: Decimal) -> bool:
    in_cloud = min(ma10, ma40) <= close <= max(ma10, ma40)
    return close < ma10 or in_cloud if side == "BUY" else close > ma10 or in_cloud


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


def _created_at(context: PlanningContext) -> datetime:
    created_at = context.market.get("created_at") or context.signal.bar_time
    if created_at is None:
        return datetime.min
    if isinstance(created_at, datetime):
        return created_at
    raise ValueError("created_at must be a datetime")
