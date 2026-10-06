"""Pure contracts shared by trade-management profiles.

Profiles calculate plans and management intentions.  Dispatching those
intentions to a broker is the responsibility of the trade manager.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import wraps
from typing import Mapping

from src.strategies.contracts import Decision
from src.trade_management.actions import AddToTrade, EntryOrderType, TradeAction
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace
from src.trade_management.models import CURRENT_ALGORITHM, CostSnapshot, ProfileSnapshot, TargetPlan, TargetPriceBasis, TradePlan, TradeState
from src.trade_management.profiles.geometry import StopBoundsConflict, StopPolicy, r_linked_ratios, resolve_stop
from src.trade_management.profiles.rules import initial_target, prioritize_management_actions, round_price


@dataclass(frozen=True)
class PlanningContext:
    """Immutable input used by a profile to turn an entry signal into a plan."""

    trade_id: str
    assignment_id: str
    instrument_id: str
    signal: Decision
    profile: ProfileSnapshot
    market: Mapping[str, object]


@dataclass(frozen=True)
class ManagementContext:
    """Immutable actual state and market data supplied to an open profile."""

    plan: TradePlan
    state: TradeState
    market: Mapping[str, object]


@dataclass(frozen=True)
class ProfileResult:
    """Pure profile output for the coordinator to persist and dispatch."""

    actions: tuple[TradeAction, ...] = ()
    state: Mapping[str, object] | None = None


def shared_management_rules(method):
    """Apply lifecycle-wide action ordering to every stateless profile."""
    def managed(self, context: ManagementContext) -> ProfileResult:
        result = method(self, context)
        if context.plan.algorithm_version == CURRENT_ALGORITHM:
            actions = []
            for action in result.actions:
                if isinstance(action, AddToTrade):
                    price = _market_decimal(context.market, "add_signal_price")
                    if price is None:
                        raise ValueError("limit addition requires its own signal price")
                    if self.NAME == "pattern_targets":
                        goal = context.plan.targets[-1].price
                        if (context.plan.side == "BUY" and price >= goal) or (context.plan.side == "SELL" and price <= goal):
                            continue
                    action = replace(action, order_type=EntryOrderType.LIMIT, limit_price=price)
                actions.append(action)
            result = ProfileResult(tuple(actions), result.state)
        return ProfileResult(
            prioritize_management_actions(
                context.state,
                result.actions,
                pending_increase=bool(context.market.get("pending_increase")),
                late_increase_fill_quantity=context.market.get("late_increase_fill_quantity"),
            ),
            result.state,
        )
    return managed


def bounded_stop_geometry(plan: TradePlan, market: Mapping[str, object]) -> TradePlan:
    """Apply the shared stop geometry to a plan a profile has already built.

    The profile's own distance is treated as the structural proposal: it is what
    the profile knows about the market, while the bounds belong to the system.
    Only the stop changes, and only the targets the profile priced in risk units
    follow it — a target named in absolute terms stays exactly where the profile
    put it.

    New plans reject incompatible bounds; legacy geometry keeps its old policy.
    """
    policy = StopPolicy.from_parameters(plan.profile.parameters)
    geometry = resolve_stop(
        plan.reference_entry,
        plan.side,
        plan.risk_per_unit,
        policy,
        atr=_market_decimal(market, "atr"),
        bar_range=_market_decimal(market, "bar_range"),
        price_step=_market_decimal(market, "price_step"),
        reject_conflict=plan.algorithm_version == CURRENT_ALGORITHM,
    )
    if plan.algorithm_version != CURRENT_ALGORITHM and geometry.price == plan.stop_price and not geometry.adjusted:
        return plan
    parameters = dict(plan.profile.parameters)
    if plan.algorithm_version == CURRENT_ALGORITHM and plan.profile.name in {"levels_rr", "atr_trend"}:
        parameters.setdefault("target_R", (1, 2))
    ratios = r_linked_ratios(parameters)
    step = _market_decimal(market, "price_step")
    direction = Decimal("1") if plan.side == "BUY" else Decimal("-1")
    targets = tuple(
        _reprice_target(target, plan, geometry.distance, ratios.get(target.target_id), step, direction, market)
        for target in plan.targets
    )
    return replace(
        plan,
        stop_price=geometry.price,
        targets=targets,
        stop_basis=geometry.basis,
    )


def _reprice_target(
    target: TargetPlan,
    plan: TradePlan,
    distance: Decimal,
    ratio: Decimal | None,
    step: Decimal | None,
    direction: Decimal,
    market: Mapping[str, object],
) -> TargetPlan:
    """Move an R-priced target with the bounded stop; leave an absolute one alone.

    The grid step recorded at plan time moves with the price, so a later rebase
    onto the filled average still counts from the same grid the plan now uses.
    """
    if target.price_basis is TargetPriceBasis.ABSOLUTE or step is None:
        return target
    if ratio is None:
        if plan.algorithm_version != CURRENT_ALGORITHM:
            return target
        ratio = direction * (target.price-plan.reference_entry) / plan.risk_per_unit
    compensation = Decimal(0)
    if plan.algorithm_version == CURRENT_ALGORITHM and plan.cost_snapshot.round_trip > 0:
        compensation = plan.cost_snapshot.round_trip * step / _market_decimal(market, "step_cost")
    raw = plan.reference_entry + direction * (distance * ratio + compensation)
    try:
        price = (round_price(raw, step, "ceiling" if plan.side == "BUY" else "floor")
                 if plan.algorithm_version == CURRENT_ALGORITHM else initial_target(raw, step, plan.side))
    except ValueError as exc:
        raise InvalidTargetPrice("target-not-ahead") from exc
    if plan.algorithm_version == CURRENT_ALGORITHM and (price <= 0 or direction * (price-plan.reference_entry) <= 0):
        raise InvalidTargetPrice("target-not-ahead")
    return replace(target, price=price, initial_step=direction * (price - plan.reference_entry))


class InvalidTargetPrice(ValueError):
    """An R-priced target is not a usable profitable price."""


def shared_planning_rules(method):
    """Bound the protective stop of every planned profile the same way.

    Wrapping ``plan`` rather than adding a step to the base class is what makes
    the rule unbypassable: a profile that returns a rejection is passed through
    untouched, and a profile that returns a plan always comes back through the
    same geometry.
    """
    @wraps(method)
    def planned(self, context: PlanningContext) -> TradePlan | ProfileResult:
        result = method(self, context)
        if isinstance(result, TradePlan):
            if context.market.get("algorithm_version") == CURRENT_ALGORITHM:
                result = replace(
                    result, algorithm_version=CURRENT_ALGORITHM,
                    entry_order_type=EntryOrderType.LIMIT,
                    cost_snapshot=CostSnapshot.from_market(context.market),
                    admission_snapshot=context.market.get("admission_snapshot", {}),
                    price_step=_market_decimal(context.market, "price_step"),
                    targets=tuple(
                        replace(t, price_basis=TargetPriceBasis.ABSOLUTE)
                        if self.NAME == "pattern_targets" else t for t in result.targets
                    ),
                )
            try:
                return bounded_stop_geometry(result, context.market)
            except StopBoundsConflict as exc:
                return ProfileResult(state={"reason": "stop-bounds-conflict", **exc.details})
            except InvalidTargetPrice:
                return ProfileResult(state={"reason": "target-not-ahead"})
        return result
    return planned


def _market_decimal(market: Mapping[str, object], name: str) -> Decimal | None:
    value = market.get(name)
    return None if value is None else Decimal(str(value))


class TradeManagementProfile(ABC):
    """Stateless contract for profile planning and trade management.

    Implementations receive views and return data only.  They deliberately do
    not accept a broker, repository, or other side-effecting dependency.
    """

    NAME: str
    REQUIRED_STRATEGY_CAPABILITIES: frozenset[str] = frozenset()

    @abstractmethod
    def plan(self, context: PlanningContext) -> TradePlan | ProfileResult:
        """Build an entry plan or return a rejected planning result."""

    @abstractmethod
    def manage(self, context: ManagementContext) -> ProfileResult:
        """Return intended actions without dispatching them."""

    def plan_with_trace(self, context: PlanningContext) -> tuple[TradePlan | ProfileResult, CalculationTrace]:
        """Return a plan plus immutable local inputs needed to independently check it."""
        result = self.plan(context)
        plan = result if isinstance(result, TradePlan) else None
        inputs = {
            "signal_price": MeasuredValue(context.signal.price, "price"),
            "market": MeasuredValue(dict(context.market), "local-market-inputs"),
            "parameters": MeasuredValue(dict(context.profile.parameters), "profile-parameters"),
        }
        policy = StopPolicy.from_parameters(context.profile.parameters)
        cap = policy.cap(_market_decimal(context.market, "atr"))
        inputs["floor"] = MeasuredValue(policy.floor(
            atr=_market_decimal(context.market, "atr"), bar_range=_market_decimal(context.market, "bar_range"),
            price_step=_market_decimal(context.market, "price_step")), "price")
        inputs["cap"] = MeasuredValue(cap, "price")
        if plan:
            inputs["rounded_distance"] = MeasuredValue(plan.risk_per_unit, "price")
            inputs["rounding_excess_over_cap"] = MeasuredValue(max(Decimal(0), plan.risk_per_unit-cap) if cap is not None else None, "price")
        output = (
            {"entry": str(plan.reference_entry), "stop": str(plan.stop_price),
             "stop_basis": str(plan.stop_basis),
             "targets": [(target.target_id, str(target.price), str(target.share)) for target in plan.targets]}
            if plan else dict(result.state or {})
        )
        reason = "planned" if plan else str(output.get("reason", "rejected"))
        return result, calculation_trace(
            f"profile.{self.NAME}.plan", inputs=inputs,
            result=MeasuredValue(output, "trade-plan"), reason=reason,
            formula="profile parameters + signal + local market inputs -> plan or rejection",
            links=TraceLinks(assignment_id=context.assignment_id, trade_id=context.trade_id,
                             signal_id=context.signal.event_id),
            outcome=TraceOutcome.ACCEPTED if plan else TraceOutcome.REJECTED,
        )

    def manage_with_trace(self, context: ManagementContext) -> tuple[ProfileResult, CalculationTrace]:
        """Return management intentions plus their local, reproducible inputs."""
        result = self.manage(context)
        output = {"actions": [type(action).__name__ for action in result.actions], "state": result.state}
        return result, calculation_trace(
            f"profile.{self.NAME}.manage",
            inputs={"market": MeasuredValue(dict(context.market), "local-market-inputs"),
                    "quantity": MeasuredValue(context.state.quantity, "contracts"),
                    "confirmed_stop": MeasuredValue(context.state.confirmed_stop or context.plan.stop_price, "price")},
            result=MeasuredValue(output, "management-intentions"),
            reason="actions-planned" if result.actions else "no-action",
            formula="profile state + confirmed position + local market inputs -> management intentions",
            links=TraceLinks(assignment_id=context.plan.assignment_id, trade_id=context.plan.trade_id,
                             signal_id=context.plan.signal_id),
            outcome=TraceOutcome.ACCEPTED,
        )
