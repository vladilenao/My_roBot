"""Decimal-based price and quantity rules shared by trade-management profiles."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping, Sequence

from src.trade_management.actions import CancelEntry, CloseTrade, ReduceTrade, TradeAction
from src.trade_management.models import TargetPlan, TradePhase, TradeState


LONG_SIDES = frozenset({"BUY", "LONG"})
SHORT_SIDES = frozenset({"SELL", "SHORT"})


@dataclass(frozen=True)
class TargetAllocation:
    """A non-zero target order quantity derived from confirmed entry fills."""

    target_id: str
    quantity: int


@dataclass(frozen=True)
class TargetAllocationResult:
    """Target orders plus volume retained for a trailing exit, when applicable."""

    targets: tuple[TargetAllocation, ...]
    trailing_quantity: int

    @property
    def trailing_activated(self) -> bool:
        return self.trailing_quantity > 0


def add_quantity(
    state: TradeState,
    parameters: Mapping[str, object],
    candidate_price: Decimal | int | str,
    side: str,
    *,
    pending_increase: bool = False,
) -> int | None:
    """Return the permitted add size, or ``None`` when shared rules reject it."""
    if pending_increase or state.phase is TradePhase.REDUCING or state.completed_target_ids:
        return None
    max_adds = _non_negative_int(parameters.get("max_adds", 0), "max_adds")
    if state.add_count >= max_adds:
        return None
    price = validate_price(candidate_price, "add_signal_price")
    average = validate_price(state.average_price, "average_price")
    profitable = price > average if _is_long(side) else price < average
    if not profitable:
        return None
    fraction = _share(parameters.get("add_fraction", "0.5"))
    quantity = int(state.quantity * fraction)
    return quantity or None


def prioritize_management_actions(
    state: TradeState,
    actions: Sequence[TradeAction],
    *,
    pending_increase: bool = False,
    late_increase_fill_quantity: int | None = None,
) -> tuple[TradeAction, ...]:
    """Apply shared exit ordering and cancel an increase after a filled reduction."""
    closes = tuple(action for action in actions if isinstance(action, CloseTrade))
    if closes:
        actions = closes
    elif late_increase_fill_quantity is not None:
        actions = tuple(actions) + (reduce_late_increase_fill(state, late_increase_fill_quantity),)
    if state.phase is TradePhase.REDUCING and pending_increase:
        actions = tuple(actions) + (
            CancelEntry(
                f"{state.trade_id}:cancel-pending-increase:{state.state_revision}",
                state.trade_id,
                state.state_revision,
                "first-reduction-filled",
            ),
        )
    return tuple(actions)


def reduce_late_increase_fill(state: TradeState, filled_quantity: int) -> ReduceTrade:
    """Create the compensating reduction for an increase filled after cancellation."""
    if state.phase is not TradePhase.REDUCING:
        raise ValueError("late increase can only be reduced after the first reduction")
    if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int) or filled_quantity <= 0:
        raise ValueError("filled_quantity must be a positive integer")
    if filled_quantity > state.quantity:
        raise ValueError("late increase fill exceeds open quantity")
    return ReduceTrade(
        f"{state.trade_id}:late-increase-reduce:{state.state_revision}",
        state.trade_id,
        state.state_revision,
        "late-increase-fill-after-cancel",
        filled_quantity,
    )


def allocate_target_quantities(
    filled_quantity: int,
    targets: Sequence[TargetPlan],
    *,
    retain_remainder_for_trailing: bool = False,
) -> TargetAllocationResult:
    """Allocate target orders from actual fills, omitting every zero-sized order.

    Standard profiles give the final target all remaining filled quantity.  ATR
    trend retains the remainder for trailing rather than creating a final TP.
    """
    if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int):
        raise ValueError("filled_quantity must be a non-negative integer")
    if filled_quantity < 0:
        raise ValueError("filled_quantity must be a non-negative integer")
    if not targets:
        raise ValueError("at least one target is required")

    remaining = filled_quantity
    allocations: list[TargetAllocation] = []
    final_index = len(targets) - 1
    for index, target in enumerate(targets):
        quantity = (
            int(filled_quantity * target.share)
            if retain_remainder_for_trailing or index != final_index
            else remaining
        )
        quantity = min(quantity, remaining)
        if quantity:
            allocations.append(TargetAllocation(target.target_id, quantity))
            remaining -= quantity

    return TargetAllocationResult(tuple(allocations), remaining)


def _decimal(value: Decimal | int | str, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return result


def _is_long(side: str) -> bool:
    normalized = side.upper()
    if normalized in LONG_SIDES:
        return True
    if normalized in SHORT_SIDES:
        return False
    raise ValueError("side must be BUY/LONG or SELL/SHORT")


def _share(value: object) -> Decimal:
    share = _decimal(value, "add_fraction")
    if not Decimal("0") < share <= Decimal("1"):
        raise ValueError("add_fraction must be in (0, 1]")
    return share


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def validate_price(price: Decimal | int | str, name: str = "price") -> Decimal:
    """Return a finite, strictly positive price."""
    result = _decimal(price, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def validate_price_step(price_step: Decimal | int | str) -> Decimal:
    return validate_price(price_step, "price_step")


def validate_stop(
    entry: Decimal | int | str, stop: Decimal | int | str, side: str
) -> tuple[Decimal, Decimal]:
    """Validate a protective stop is strictly on the loss side of entry."""
    entry_price = validate_price(entry, "entry")
    stop_price = validate_price(stop, "stop")
    if _is_long(side):
        valid = stop_price < entry_price
    else:
        valid = stop_price > entry_price
    if not valid:
        raise ValueError("stop must be on the loss side of entry")
    return entry_price, stop_price


def round_price(
    price: Decimal | int | str, price_step: Decimal | int | str, rounding: str
) -> Decimal:
    """Round a finite price to a valid price step using floor or ceiling."""
    value = validate_price(price)
    step = validate_price_step(price_step)
    modes = {"floor": ROUND_FLOOR, "ceiling": ROUND_CEILING}
    try:
        mode = modes[rounding]
    except KeyError as exc:
        raise ValueError("rounding must be floor or ceiling") from exc
    return (value / step).to_integral_value(rounding=mode) * step


def initial_stop(
    stop: Decimal | int | str, price_step: Decimal | int | str, side: str
) -> Decimal:
    """Round initial protection away from entry: down for LONG, up for SHORT."""
    return round_price(stop, price_step, "floor" if _is_long(side) else "ceiling")


def initial_target(
    target: Decimal | int | str, price_step: Decimal | int | str, side: str
) -> Decimal:
    """Round initial targets toward entry: down for LONG, up for SHORT."""
    return round_price(target, price_step, "floor" if _is_long(side) else "ceiling")


def cost_aware_break_even(
    average_price: Decimal | int | str,
    side: str,
    price_step: Decimal | int | str,
    step_cost: Decimal | int | str,
    quantity: int,
    entry_cost: Decimal | int | str = Decimal("0"),
    exit_cost: Decimal | int | str = Decimal("0"),
    slippage_cost: Decimal | int | str = Decimal("0"),
) -> Decimal:
    """Return the stop price that covers allocated costs after step rounding.

    Costs are monetary totals for the remaining position.  The result rounds up
    for LONG and down for SHORT so closing at that tick does not under-cover them.
    """
    entry = validate_price(average_price, "average_price")
    step = validate_price_step(price_step)
    tick_cost = validate_price(step_cost, "step_cost")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    costs = sum(
        (_decimal(entry_cost, "entry_cost"), _decimal(exit_cost, "exit_cost"),
         _decimal(slippage_cost, "slippage_cost")),
        Decimal("0"),
    )
    if costs < 0:
        raise ValueError("costs cannot be negative")
    price_offset = costs * step / (tick_cost * quantity)
    raw_break_even = entry + price_offset if _is_long(side) else entry - price_offset
    return round_price(raw_break_even, step, "ceiling" if _is_long(side) else "floor")
