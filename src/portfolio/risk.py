"""Pure portfolio risk limits for confirmed trade state.

Sizing, margin checks, and reservations intentionally belong to later layers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping

from src.trade_management.actions import CancelEntry, CloseTrade, ReduceTrade, TradeAction
from src.trade_management.audit import CalculationTrace, MeasuredValue, TraceLinks, TraceOutcome, calculation_trace


def _require_decimal(value: Decimal, name: str, *, non_negative: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if non_negative and value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class RiskLimits:
    """Risk caps as percentages of the current account budget base."""

    per_trade: Decimal
    per_instrument: Decimal
    per_group: Mapping[str, Decimal]
    portfolio: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("per_trade", self.per_trade),
            ("per_instrument", self.per_instrument),
            ("portfolio", self.portfolio),
        ):
            _require_decimal(value, name)
            if value > Decimal("100"):
                raise ValueError(f"{name} cannot exceed 100 percent")
        for group, value in self.per_group.items():
            if not group:
                raise ValueError("risk group name is required")
            _require_decimal(value, f"group limit {group!r}")
            if value > Decimal("100"):
                raise ValueError(f"group limit {group!r} cannot exceed 100 percent")


@dataclass(frozen=True)
class RiskTrade:
    """Confirmed state needed to measure one trade's complete loss budget."""

    trade_id: str
    instrument_id: str
    groups: frozenset[str]
    side: str
    quantity: int
    average_price: Decimal
    stop_price: Decimal
    price_step: Decimal
    step_cost: Decimal
    paid_fees: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    expected_exit_cost: Decimal = Decimal("0")
    slippage_allowance: Decimal = Decimal("0")
    factual_protection_breached: bool = False

    def __post_init__(self) -> None:
        if not self.trade_id or not self.instrument_id:
            raise ValueError("trade_id and instrument_id are required")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for name, value in (
            ("average_price", self.average_price),
            ("stop_price", self.stop_price),
            ("price_step", self.price_step),
            ("step_cost", self.step_cost),
            ("paid_fees", self.paid_fees),
            ("expected_exit_cost", self.expected_exit_cost),
            ("slippage_allowance", self.slippage_allowance),
        ):
            _require_decimal(value, name)
        _require_decimal(self.realized_pnl, "realized_pnl", non_negative=False)
        if self.average_price <= 0 or self.stop_price <= 0 or self.price_step <= 0 or self.step_cost <= 0:
            raise ValueError("prices, price_step, and step_cost must be positive")
        if not isinstance(self.factual_protection_breached, bool):
            raise ValueError("factual_protection_breached must be a boolean")
        if not self.protection_is_loss_side and not self.factual_protection_breached:
            raise ValueError("stop must be on the loss side of the average price")

    @property
    def protection_is_loss_side(self) -> bool:
        return (self.side == "BUY" and self.stop_price < self.average_price) or (
            self.side == "SELL" and self.stop_price > self.average_price
        )


@dataclass(frozen=True)
class RiskAddition:
    """Requested increase and its per-contract costs and margin requirement."""

    trade_id: str
    entry_price: Decimal
    max_total_quantity: int
    entry_fee_per_contract: Decimal = Decimal("0")
    expected_exit_cost_per_contract: Decimal = Decimal("0")
    slippage_allowance_per_contract: Decimal = Decimal("0")
    margin_per_contract: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise ValueError("trade_id is required")
        if isinstance(self.max_total_quantity, bool) or not isinstance(self.max_total_quantity, int):
            raise ValueError("max_total_quantity must be an integer")
        if self.max_total_quantity < 1:
            raise ValueError("max_total_quantity must be positive")
        for name, value in (
            ("entry_price", self.entry_price),
            ("entry_fee_per_contract", self.entry_fee_per_contract),
            ("expected_exit_cost_per_contract", self.expected_exit_cost_per_contract),
            ("slippage_allowance_per_contract", self.slippage_allowance_per_contract),
            ("margin_per_contract", self.margin_per_contract),
        ):
            _require_decimal(value, name)
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")


@dataclass(frozen=True)
class TradeRisk:
    trade_id: str
    stop_distance: Decimal
    remaining_loss: Decimal
    full_loss_budget: Decimal


@dataclass(frozen=True)
class RiskLimitViolation:
    scope: str
    key: str
    used: Decimal
    limit: Decimal


@dataclass(frozen=True)
class PortfolioRiskReport:
    budget_base: Decimal
    trade_risks: Mapping[str, TradeRisk]
    instrument_risks: Mapping[str, Decimal]
    group_risks: Mapping[str, Decimal]
    portfolio_risk: Decimal
    violations: tuple[RiskLimitViolation, ...]

    @property
    def allowed(self) -> bool:
        return not self.violations


class PortfolioRiskManager:
    """Calculates risk caps and permitted increases without reserving resources."""

    def evaluate(
        self,
        *,
        balance: Decimal,
        equity: Decimal,
        limits: RiskLimits,
        trades: tuple[RiskTrade, ...],
    ) -> PortfolioRiskReport:
        _require_decimal(balance, "balance", non_negative=False)
        _require_decimal(equity, "equity", non_negative=False)
        budget_base = max(Decimal("0"), min(balance, equity))
        trade_risks: dict[str, TradeRisk] = {}
        instrument_risks: dict[str, Decimal] = {}
        group_risks: dict[str, Decimal] = {}

        for trade in trades:
            if trade.trade_id in trade_risks:
                raise ValueError(f"duplicate trade_id {trade.trade_id!r}")
            risk = self.trade_risk(trade)
            trade_risks[trade.trade_id] = risk
            instrument_risks[trade.instrument_id] = instrument_risks.get(trade.instrument_id, Decimal("0")) + risk.full_loss_budget
            for group in trade.groups:
                group_risks[group] = group_risks.get(group, Decimal("0")) + risk.full_loss_budget

        portfolio_risk = sum((risk.full_loss_budget for risk in trade_risks.values()), Decimal("0"))
        violations: list[RiskLimitViolation] = []
        self._check(violations, "trade", trade_risks, budget_base * limits.per_trade / Decimal("100"), "full_loss_budget")
        self._check(violations, "instrument", instrument_risks, budget_base * limits.per_instrument / Decimal("100"))
        for group, used in group_risks.items():
            if group in limits.per_group:
                self._check(violations, "group", {group: used}, budget_base * limits.per_group[group] / Decimal("100"))
        self._check(violations, "portfolio", {"portfolio": portfolio_risk}, budget_base * limits.portfolio / Decimal("100"))
        return PortfolioRiskReport(
            budget_base=budget_base,
            trade_risks=trade_risks,
            instrument_risks=instrument_risks,
            group_risks=group_risks,
            portfolio_risk=portfolio_risk,
            violations=tuple(violations),
        )

    @staticmethod
    def trade_risk(trade: RiskTrade) -> TradeRisk:
        direction = Decimal("1") if trade.side == "BUY" else Decimal("-1")
        stop_distance = direction * (trade.average_price - trade.stop_price)
        stop_loss = max(Decimal("0"), stop_distance * trade.step_cost / trade.price_step * trade.quantity)
        remaining_loss = stop_loss + trade.expected_exit_cost + trade.slippage_allowance
        full_loss_budget = max(Decimal("0"), remaining_loss + trade.paid_fees - trade.realized_pnl)
        return TradeRisk(trade.trade_id, stop_distance, remaining_loss, full_loss_budget)

    def maximum_additional_quantity(
        self,
        *,
        balance: Decimal,
        equity: Decimal,
        limits: RiskLimits,
        trades: tuple[RiskTrade, ...],
        addition: RiskAddition,
        open_margin: Decimal = Decimal("0"),
        pending_margin: Decimal = Decimal("0"),
    ) -> int:
        """Return the largest allowed integer increase for an existing trade.

        This is deliberately a pure sizing calculation. Reserving the accepted
        risk and margin is a separate transactional operation.
        """
        _require_decimal(open_margin, "open_margin")
        _require_decimal(pending_margin, "pending_margin")
        _require_decimal(balance, "balance", non_negative=False)
        _require_decimal(equity, "equity", non_negative=False)
        trade_by_id = {trade.trade_id: trade for trade in trades}
        if len(trade_by_id) != len(trades):
            raise ValueError("duplicate trade_id")
        try:
            current = trade_by_id[addition.trade_id]
        except KeyError as exc:
            raise ValueError(f"unknown trade_id {addition.trade_id!r}") from exc
        if (current.side == "BUY" and addition.entry_price <= current.stop_price) or (
            current.side == "SELL" and addition.entry_price >= current.stop_price
        ):
            raise ValueError("addition entry must be on the profitable side of the stop")
        maximum = addition.max_total_quantity - current.quantity
        if maximum <= 0:
            return 0
        budget_base = max(Decimal("0"), min(balance, equity))
        current_report = self.evaluate(balance=balance, equity=equity, limits=limits, trades=trades)
        if not current_report.allowed:
            return 0
        direction = Decimal("1") if current.side == "BUY" else Decimal("-1")
        value_per_point = current.step_cost / current.price_step
        current_risk = current_report.trade_risks[current.trade_id]

        def allowed(quantity: int) -> bool:
            if open_margin + pending_margin + addition.margin_per_contract * quantity > budget_base:
                return False
            stop_loss = (
                direction * (current.average_price - current.stop_price) * value_per_point * current.quantity
                + direction * (addition.entry_price - current.stop_price) * value_per_point * quantity
            )
            remaining_loss = (
                stop_loss
                + current.expected_exit_cost
                + addition.expected_exit_cost_per_contract * quantity
                + current.slippage_allowance
                + addition.slippage_allowance_per_contract * quantity
            )
            full_loss_budget = max(
                Decimal("0"), remaining_loss + current.paid_fees + addition.entry_fee_per_contract * quantity - current.realized_pnl,
            )
            difference = full_loss_budget - current_risk.full_loss_budget
            if full_loss_budget > budget_base * limits.per_trade / Decimal("100"):
                return False
            if current_report.instrument_risks[current.instrument_id] + difference > budget_base * limits.per_instrument / Decimal("100"):
                return False
            for group in current.groups:
                if group in limits.per_group and current_report.group_risks[group] + difference > budget_base * limits.per_group[group] / Decimal("100"):
                    return False
            return current_report.portfolio_risk + difference <= budget_base * limits.portfolio / Decimal("100")

        low, high = 0, maximum
        while low < high:
            candidate = (low + high + 1) // 2
            if allowed(candidate):
                low = candidate
            else:
                high = candidate - 1
        return low

    def maximum_additional_quantity_with_trace(self, **kwargs: object) -> tuple[int, CalculationTrace]:
        """Size an increase and expose budgets, costs, and floor result for audit."""
        quantity = self.maximum_additional_quantity(**kwargs)
        addition = kwargs["addition"]
        assert isinstance(addition, RiskAddition)
        current = next(trade for trade in kwargs["trades"] if trade.trade_id == addition.trade_id)
        assert isinstance(current, RiskTrade)
        balance = kwargs["balance"]
        equity = kwargs["equity"]
        assert isinstance(balance, Decimal) and isinstance(equity, Decimal)
        budget = max(Decimal("0"), min(balance, equity))
        return quantity, calculation_trace(
            "portfolio.maximum_additional_quantity",
            inputs={
                "balance": MeasuredValue(balance, "RUB"), "equity": MeasuredValue(equity, "RUB"),
                "budget_base": MeasuredValue(budget, "RUB"),
                "entry_price": MeasuredValue(addition.entry_price, "price"),
                "stop_price": MeasuredValue(current.stop_price, "price"),
                "price_step": MeasuredValue(current.price_step, "price"),
                "step_cost": MeasuredValue(current.step_cost, "RUB/tick"),
                "entry_fee": MeasuredValue(addition.entry_fee_per_contract, "RUB/contracts"),
                "exit_cost": MeasuredValue(addition.expected_exit_cost_per_contract, "RUB/contracts"),
                "slippage": MeasuredValue(addition.slippage_allowance_per_contract, "RUB/contracts"),
                "margin": MeasuredValue(addition.margin_per_contract, "RUB/contracts"),
            }, result=MeasuredValue(quantity, "contracts"), reason="largest-permitted-integer-quantity",
            formula="largest q satisfying trade/instrument/group/portfolio risk and margin limits",
            links=TraceLinks(trade_id=current.trade_id), outcome=TraceOutcome.ACCEPTED if quantity else TraceOutcome.REJECTED,
        )

    def corrective_actions_after_increasing_fill(
        self,
        *,
        balance: Decimal,
        equity: Decimal,
        limits: RiskLimits,
        trades: tuple[RiskTrade, ...],
        filled_trade: RiskTrade,
        unfilled_increase_quantity: int,
        state_revision: int,
    ) -> tuple[TradeAction, ...]:
        """Cancel an unsafe increase remainder and reduce only its owning trade.

        ``filled_trade`` must contain the factual quantity, average price, and
        confirmed stop after the broker fill.  The stop is deliberately kept
        fixed while searching for a safe remaining size: widening it to retain
        a larger position would accept additional risk.
        """
        if (
            isinstance(unfilled_increase_quantity, bool)
            or not isinstance(unfilled_increase_quantity, int)
            or unfilled_increase_quantity < 0
        ):
            raise ValueError("unfilled_increase_quantity must be a non-negative integer")
        if isinstance(state_revision, bool) or not isinstance(state_revision, int) or state_revision < 0:
            raise ValueError("state_revision must be a non-negative integer")
        if not any(trade.trade_id == filled_trade.trade_id for trade in trades):
            raise ValueError("filled_trade must be included in trades")

        if not filled_trade.protection_is_loss_side:
            actions: list[TradeAction] = []
            if unfilled_increase_quantity:
                actions.append(CancelEntry(
                    f"{filled_trade.trade_id}:cancel-increase:{state_revision}",
                    filled_trade.trade_id,
                    state_revision,
                    "factual-increase-fill-invalidates-protection",
                ))
            actions.append(CloseTrade(
                f"{filled_trade.trade_id}:close-invalid-protection:{state_revision}",
                filled_trade.trade_id,
                state_revision,
                "factual-increase-fill-invalidates-protection",
            ))
            return tuple(actions)

        if self.evaluate(balance=balance, equity=equity, limits=limits, trades=trades).allowed:
            return ()

        actions: list[TradeAction] = []
        if unfilled_increase_quantity:
            actions.append(CancelEntry(
                f"{filled_trade.trade_id}:cancel-increase:{state_revision}",
                filled_trade.trade_id,
                state_revision,
                "factual-increase-fill-violates-risk",
            ))

        safe_quantity = 0
        for quantity in range(filled_trade.quantity - 1, 0, -1):
            reduced_trade = replace(filled_trade, quantity=quantity)
            candidate_trades = tuple(
                reduced_trade if trade.trade_id == filled_trade.trade_id else trade
                for trade in trades
            )
            if self.evaluate(
                balance=balance, equity=equity, limits=limits, trades=candidate_trades
            ).allowed:
                safe_quantity = quantity
                break

        if safe_quantity:
            actions.append(ReduceTrade(
                f"{filled_trade.trade_id}:reduce-over-risk:{state_revision}",
                filled_trade.trade_id,
                state_revision,
                "factual-increase-fill-violates-risk",
                filled_trade.quantity - safe_quantity,
            ))
        else:
            actions.append(CloseTrade(
                f"{filled_trade.trade_id}:close-over-risk:{state_revision}",
                filled_trade.trade_id,
                state_revision,
                "factual-increase-fill-violates-risk",
            ))
        return tuple(actions)

    @staticmethod
    def _check(
        violations: list[RiskLimitViolation],
        scope: str,
        values: Mapping[str, Decimal | TradeRisk],
        limit: Decimal,
        attribute: str | None = None,
    ) -> None:
        for key, value in values.items():
            used = getattr(value, attribute) if attribute else value
            if used > limit:
                violations.append(RiskLimitViolation(scope, key, used, limit))
