"""Money view of an admitted plan.

A profile knows prices and multipliers but not the contract: the price of one
point, how many contracts the risk budget actually paid for, and what entry,
exit and slippage cost.  Those three facts live in ``TradeManager``, so this is
where a plan finally becomes quotable in the account currency — and the only
place that knows a plan's money is the money of the trade that was admitted.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from src.trade_management.models import CURRENT_ALGORITHM, PlanEconomics, TradePlan
from src.trade_management.profiles.rules import allocate_target_quantities

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0")


def value_per_point(price_step: Decimal | None, step_cost: Decimal | None) -> Decimal:
    """Currency value of a one-step price move, the basis of every conversion."""
    if price_step is None or price_step <= 0 or step_cost is None or step_cost <= 0:
        return ZERO
    return step_cost / price_step


def round_trip_costs(market: dict) -> Decimal:
    """Entry, exit and slippage charged for one contract on a complete round trip."""
    return sum(
        (Decimal(str(market.get(name) or ZERO)) for name in ("entry_cost", "exit_cost", "slippage_cost")),
        ZERO,
    )


def reward_amount(plan: TradePlan, quantity: int, per_point: Decimal) -> Decimal:
    """Planned gross profit of the whole position: every target share at its own move."""
    direction = Decimal("1") if plan.side == "BUY" else Decimal("-1")
    total = ZERO
    for target in plan.targets:
        move = abs(direction * (target.price - plan.reference_entry))
        total += target.share * move * per_point * quantity
    return total


def plan_economics(
    plan: TradePlan,
    *,
    quantity: int,
    price_step: Decimal | None,
    step_cost: Decimal | None,
    market: dict,
) -> PlanEconomics:
    """Attach the money view of the admitted position to its plan.

    Costs are counted for the quantity that was actually admitted, not for one
    contract, so a position trimmed by the risk budget reports the costs it will
    really pay.  The payoff ratio is what the trader is left with after those
    costs, which is why the costs subtract from the reward rather than from the
    risk: the risk is what the stop already measures.
    """
    per_point = value_per_point(price_step, step_cost)
    per_unit_risk = plan.risk_per_unit * per_point
    risk_amount = per_unit_risk * quantity
    if plan.algorithm_version == CURRENT_ALGORITHM:
        allocations = (
            allocate_target_quantities(
                quantity, plan.targets,
                retain_remainder_for_trailing=plan.profile.name == "atr_trend",
            ) if plan.targets else None
        )
        quantities = {} if allocations is None else {a.target_id: a.quantity for a in allocations.targets}
        direction = Decimal(1) if plan.side == "BUY" else Decimal(-1)
        fixed = sum((
            direction * (t.price - plan.reference_entry) * per_point * quantities.get(t.target_id, 0)
            for t in plan.targets
        ), ZERO)
        complete = allocations is not None and allocations.trailing_quantity == 0
        costs = plan.cost_snapshot.round_trip * quantity
        reward = fixed if complete else None
        return PlanEconomics(
            quantity=quantity, risk_amount=risk_amount, reward_amount=reward,
            costs_amount=costs,
            payoff_ratio=None if reward is None or risk_amount <= 0 else (reward - costs) / risk_amount,
            fixed_reward_amount=fixed, fixed_quantity=sum(quantities.values()),
            target_quantities=quantities, slippage_amount=plan.cost_snapshot.slippage * quantity,
        )
    reward = reward_amount(plan, quantity, per_point)
    costs = round_trip_costs(market) * quantity
    ratio = None
    if risk_amount > 0 and plan.expected_r is not None:
        ratio = ((reward - costs) / risk_amount).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return PlanEconomics(
        quantity=quantity,
        risk_amount=risk_amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        reward_amount=reward.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        costs_amount=costs.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        payoff_ratio=ratio,
    )
