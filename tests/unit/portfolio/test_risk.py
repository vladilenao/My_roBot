from decimal import Decimal

import pytest

from src.portfolio import PortfolioRiskManager, RiskAddition, RiskLimits, RiskTrade
from src.trade_management.actions import CancelEntry, CloseTrade, MoveStop, ReduceTrade


D = Decimal


def _limits(**changes: object) -> RiskLimits:
    values = {
        "per_trade": D("50"),
        "per_instrument": D("60"),
        "per_group": {"energy": D("70")},
        "portfolio": D("80"),
    }
    values.update(changes)
    return RiskLimits(**values)


def _trade(**changes: object) -> RiskTrade:
    values = {
        "trade_id": "trade-1",
        "instrument_id": "NG",
        "groups": frozenset({"energy"}),
        "side": "BUY",
        "quantity": 2,
        "average_price": D("100"),
        "stop_price": D("98"),
        "price_step": D("1"),
        "step_cost": D("100"),
    }
    values.update(changes)
    return RiskTrade(**values)


class TestPortfolioRiskManager:
    def test_uses_lower_of_balance_and_equity_as_budget_base(self):
        report = PortfolioRiskManager().evaluate(
            balance=D("1000"), equity=D("800"), limits=_limits(portfolio=D("100")), trades=(_trade(),)
        )

        assert report.budget_base == D("800")

    def test_calculates_remaining_and_full_trade_loss_with_all_costs(self):
        report = PortfolioRiskManager().evaluate(
            balance=D("1000"), equity=D("1000"), limits=_limits(),
            trades=(_trade(paid_fees=D("10"), expected_exit_cost=D("8"), slippage_allowance=D("2"), realized_pnl=D("50")),),
        )

        risk = report.trade_risks["trade-1"]
        assert risk.remaining_loss == D("410")
        assert risk.full_loss_budget == D("370")

    def test_profitable_trade_does_not_net_another_trade_loss(self):
        report = PortfolioRiskManager().evaluate(
            balance=D("1000"), equity=D("1000"), limits=_limits(portfolio=D("30")),
            trades=(
                _trade(trade_id="winner", realized_pnl=D("500")),
                _trade(trade_id="loser", instrument_id="BR", groups=frozenset(), realized_pnl=D("0")),
            ),
        )

        assert report.trade_risks["winner"].full_loss_budget == D("0")
        assert report.trade_risks["loser"].full_loss_budget == D("400")
        assert report.portfolio_risk == D("400")
        assert [(item.scope, item.key) for item in report.violations] == [("portfolio", "portfolio")]

    def test_enforces_trade_instrument_group_and_portfolio_limits(self):
        report = PortfolioRiskManager().evaluate(
            balance=D("1000"), equity=D("1000"),
            limits=_limits(per_trade=D("30"), per_instrument=D("35"), per_group={"energy": D("35")}, portfolio=D("35")),
            trades=(_trade(),),
        )

        assert {(item.scope, item.key) for item in report.violations} == {
            ("trade", "trade-1"), ("instrument", "NG"), ("group", "energy"), ("portfolio", "portfolio"),
        }

    def test_short_stop_distance_is_positive(self):
        risk = PortfolioRiskManager.trade_risk(_trade(side="SELL", stop_price=D("104")))

        assert risk.stop_distance == D("4")
        assert risk.remaining_loss == D("800")

    def test_rejects_a_stop_on_the_profitable_side(self):
        with pytest.raises(ValueError, match="loss side"):
            _trade(stop_price=D("101"))

    def test_sizes_to_two_contracts_from_1000_budget_and_420_risk_per_contract(self):
        quantity = PortfolioRiskManager().maximum_additional_quantity(
            balance=D("1000"), equity=D("1000"),
            limits=_limits(per_trade=D("100"), per_instrument=D("100"), per_group={"energy": D("100")}, portfolio=D("100")),
            trades=(_trade(quantity=1, average_price=D("100"), stop_price=D("96"), realized_pnl=D("400")),),
            addition=RiskAddition(
                trade_id="trade-1", entry_price=D("100"), max_total_quantity=10,
                entry_fee_per_contract=D("5"), expected_exit_cost_per_contract=D("7"),
                slippage_allowance_per_contract=D("8"), margin_per_contract=D("333"),
            ),
        )

        assert D("400") + D("5") + D("7") + D("8") == D("420")
        assert D("1000") // D("420") == 2
        assert quantity == 2

    def test_addition_is_limited_by_free_margin(self):
        quantity = PortfolioRiskManager().maximum_additional_quantity(
            balance=D("1000"), equity=D("1000"),
            limits=_limits(per_trade=D("100"), per_instrument=D("100"), per_group={"energy": D("100")}, portfolio=D("100")),
            trades=(_trade(quantity=1, realized_pnl=D("400")),),
            addition=RiskAddition(trade_id="trade-1", entry_price=D("100"), max_total_quantity=10, margin_per_contract=D("600")),
            open_margin=D("200"),
        )

        assert quantity == 1

    def test_addition_uses_resulting_position_risk(self):
        quantity = PortfolioRiskManager().maximum_additional_quantity(
            balance=D("1000"), equity=D("1000"), limits=_limits(per_trade=D("50"), per_instrument=D("100"), portfolio=D("100")),
            trades=(_trade(side="SELL", quantity=2, average_price=D("100"), stop_price=D("101")),),
            addition=RiskAddition(trade_id="trade-1", entry_price=D("98"), max_total_quantity=3),
        )

        assert quantity == 1
        assert D("2") * D("1") * D("100") + D("1") * D("3") * D("100") == D("500")

    def test_gap_fill_over_risk_cancels_remainder_and_reduces_its_own_trade(self):
        manager = PortfolioRiskManager()
        factual_trade = _trade(quantity=2, average_price=D("105"), stop_price=D("98"))

        actions = manager.corrective_actions_after_increasing_fill(
            balance=D("1000"), equity=D("1000"),
            limits=_limits(per_trade=D("100"), per_instrument=D("100"), portfolio=D("100")),
            trades=(factual_trade,), filled_trade=factual_trade,
            unfilled_increase_quantity=1, state_revision=7,
        )

        assert actions == (
            CancelEntry("trade-1:cancel-increase:7", "trade-1", 7, "factual-increase-fill-violates-risk"),
            ReduceTrade("trade-1:reduce-over-risk:7", "trade-1", 7, "factual-increase-fill-violates-risk", 1),
        )

    def test_gap_fill_that_invalidates_protection_closes_without_widening_stop(self):
        manager = PortfolioRiskManager()
        factual_trade = _trade(
            quantity=1, average_price=D("95"), stop_price=D("98"), factual_protection_breached=True,
        )

        actions = manager.corrective_actions_after_increasing_fill(
            balance=D("1000"), equity=D("1000"),
            limits=_limits(per_trade=D("100"), per_instrument=D("100"), portfolio=D("100")),
            trades=(factual_trade,), filled_trade=factual_trade,
            unfilled_increase_quantity=1, state_revision=8,
        )

        assert actions == (
            CancelEntry("trade-1:cancel-increase:8", "trade-1", 8, "factual-increase-fill-invalidates-protection"),
            CloseTrade("trade-1:close-invalid-protection:8", "trade-1", 8, "factual-increase-fill-invalidates-protection"),
        )
        assert factual_trade.stop_price == D("98")
        assert not any(isinstance(action, MoveStop) for action in actions)
