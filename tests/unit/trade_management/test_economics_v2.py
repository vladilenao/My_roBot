"""Версия плана, целочисленная экономика и восстановление исходных уровней."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.actions import EntryOrderType, OpenTrade
from src.trade_management.economics import plan_economics
from src.trade_management.manager import TradeManager, _action_from_payload, _action_payload
from src.trade_management.models import (
    CURRENT_ALGORITHM, LEGACY_ALGORITHM, CostSnapshot, PortfolioBudgetPolicy,
    ProfileSnapshot, TargetPlan, TargetPriceBasis, TradePlan, rebase_on_average,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _plan(profile="levels_rr", *, costs=None, basis=TargetPriceBasis.R, admission=None):
    return TradePlan(
        "v2-trade", "assignment", "instrument", "BUY", "signal", D(100), D(96),
        (TargetPlan("tp-1", D(104), D("0.5"), price_basis=basis),
         TargetPlan("tp-2", D(108), D("0.5"), price_basis=basis)),
        ProfileSnapshot(profile, "1", {"target_R": [1, 2], "shares": [0.5, 0.5]}), NOW,
        algorithm_version=CURRENT_ALGORITHM, cost_snapshot=costs or CostSnapshot(D(0), D(0)),
        entry_order_type=EntryOrderType.LIMIT,
        admission_snapshot=admission if admission is not None else {"portfolio_pct": D(2)},
    )


def _economics(plan, quantity):
    # Текущая конфигурация намеренно отличается от сохранённой оценки.
    return plan_economics(plan, quantity=quantity, price_step=D(1), step_cost=D(100),
                          market={"entry_cost": D(999), "exit_cost": D(999), "slippage_cost": D(999)})


@pytest.mark.parametrize("quantity,reward,alloc", [
    (1, D(800), {"tp-2": 1}),
    (2, D(1200), {"tp-1": 1, "tp-2": 1}),
    (3, D(2000), {"tp-1": 1, "tp-2": 2}),
])
def test_full_reward_uses_real_integer_contracts(quantity, reward, alloc):
    plan = _plan()
    money = _economics(plan, quantity)
    assert money.risk_amount == D(400) * quantity
    assert money.reward_amount == reward
    assert dict(money.target_quantities) == alloc
    assert money.fixed_quantity == quantity
    assert money.costs_amount == 0
    assert replace(plan, economics=money).expected_r == reward / (D(400) * quantity)


def test_cost_snapshot_survives_changed_market_and_ratios_are_not_rounded():
    plan = _plan(costs=CostSnapshot(D("1.5"), D(1)))
    money = _economics(plan, 3)
    assert money.costs_amount == 12
    assert money.slippage_amount == 3
    assert money.budget_risk_amount == 1212
    assert money.net_reward_amount == 1988
    assert money.payoff_ratio == D(1988) / D(1200)


def test_trailing_remainder_is_not_a_known_full_payoff():
    plan = _plan("atr_trend")
    plan = replace(plan, targets=tuple(replace(t, share=D("0.25")) for t in plan.targets))
    money = _economics(plan, 8)
    assert dict(money.target_quantities) == {"tp-1": 2, "tp-2": 2}
    assert money.fixed_quantity == 4
    assert money.fixed_reward_amount == 2400
    assert money.reward_amount is None and money.payoff_ratio is None
    assert replace(plan, economics=money).expected_r is None


def test_cloud_has_known_risk_and_costs_but_no_total_reward():
    plan = replace(_plan("ma_cloud", costs=CostSnapshot(D("1.5"), D(1))), targets=())
    money = _economics(plan, 10)
    assert money.risk_amount == 4000 and money.costs_amount == 40
    assert money.fixed_quantity == 0 and money.fixed_reward_amount == 0
    assert money.reward_amount is None and money.payoff_ratio is None


def test_snapshot_is_detached_and_immutable():
    supplied = {"portfolio_pct": D(2), "extra": [1, 2]}
    plan = _plan(admission=supplied)
    supplied["portfolio_pct"] = D(99)
    supplied["extra"].append(3)
    assert plan.admission_snapshot["portfolio_pct"] == 2
    assert plan.admission_snapshot["extra"] == (1, 2)
    with pytest.raises(TypeError):
        plan.admission_snapshot["portfolio_pct"] = D(3)


def test_rebase_keeps_absolute_prices_and_original_plan():
    plan = _plan("pattern_targets", basis=TargetPriceBasis.ABSOLUTE)
    active = rebase_on_average(plan, D(99))
    assert [t.price for t in active.targets] == [D(104), D(108)]
    assert active.stop_price == 95
    assert plan.reference_entry == 100 and plan.stop_price == 96
    assert [t.price for t in rebase_on_average(_plan(), D(99)).targets] == [D(103), D(107)]


def test_payload_restores_plan_instead_of_mutable_target_rows(tmp_path):
    plan = _plan(costs=CostSnapshot(D("1.5"), D(1)))
    plan = replace(plan, economics=_economics(plan, 3))
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, object())
        action = OpenTrade("entry", plan.trade_id, 0, "entry", 3, EntryOrderType.LIMIT, D(100))
        assert manager.submit_plan(plan, action, price_step=D(1), step_cost=D(100))
        with storage.transaction() as connection:
            connection.execute("UPDATE targets SET price='103' WHERE target_id='tp-1'")
        restored = storage.load_trade(plan.trade_id)
        assert restored.plan == plan
        assert restored.state.target_prices["tp-1"] == 103
        assert restored.plan.targets[0].price == 104
        row = storage.connection.execute("SELECT payload_json FROM outbox WHERE command_id='entry'").fetchone()
        assert '"limit_price": "100"' in row[0]


def test_limit_action_roundtrip_and_legacy_payload():
    action = OpenTrade("entry", "trade", 0, "entry", 2, EntryOrderType.LIMIT, D("113.8"))
    assert _action_from_payload(_action_payload(action)) == action
    legacy = OpenTrade("old", "trade", 0, "entry", 2)
    payload = _action_payload(legacy)
    assert "limit_price" not in payload and "order_type" not in payload
    assert _action_from_payload(payload).order_type is EntryOrderType.MARKET


def test_missing_limit_is_rejected_before_dispatch():
    with pytest.raises(ValueError, match="limit_price"):
        OpenTrade("entry", "trade", 0, "entry", 1, EntryOrderType.LIMIT)


def test_new_version_cannot_be_silently_restored_as_market():
    with pytest.raises(ValueError, match="limit entry"):
        replace(_plan(), entry_order_type=EntryOrderType.MARKET)
    with pytest.raises(ValueError, match="algorithm_version"):
        replace(_plan(), algorithm_version="unknown-version")


def test_legacy_keeps_old_rebase_semantics():
    plan = replace(_plan("pattern_targets", basis=TargetPriceBasis.ABSOLUTE),
                   algorithm_version=LEGACY_ALGORITHM, entry_order_type=EntryOrderType.MARKET,
                   cost_snapshot=None)
    assert rebase_on_average(plan, D(99)).targets[0].price == 103


def test_global_policy_uses_conservative_capital():
    policy = PortfolioBudgetPolicy()
    assert policy.budget(D(100000), D(105000)) == 2000
    assert policy.budget(D(100000), D(98000)) == 1960
    assert policy.budget(D(-1), D(10)) == 0
    assert PortfolioBudgetPolicy(D(0)).budget(D(100000), D(100000)) == 0
