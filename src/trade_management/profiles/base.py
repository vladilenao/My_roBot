"""Pure contracts shared by trade-management profiles.

Profiles calculate plans and management intentions.  Dispatching those
intentions to a broker is the responsibility of the trade manager.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from src.strategies.contracts import Decision
from src.trade_management.actions import TradeAction
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace
from src.trade_management.models import ProfileSnapshot, TradePlan, TradeState
from src.trade_management.profiles.rules import prioritize_management_actions


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
        output = (
            {"entry": str(plan.reference_entry), "stop": str(plan.stop_price),
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
