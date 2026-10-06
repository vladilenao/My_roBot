"""Денежная экономика плана и два отказа, которые она вызывает.

Деньги считаются для фактически допущенного объёма: риск-бюджет может урезать
вход, и тогда план обязан показывать цифры именно этой сделки, а не той, что
профилю хотелось бы открыть.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.portfolio.models import ContractMeta
from src.portfolio.risk import RiskLimits
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.economics import plan_economics
from src.trade_management.manager import TradeManager
from src.trade_management.models import (
    PlanEconomics,
    ProfileSnapshot,
    TargetPlan,
    TradePlan,
)

STEP = Decimal("0.001")
STEP_COST = Decimal("8.4")
META = ContractMeta(
    ticker="NGV6", price_step=float(STEP), step_cost=float(STEP_COST), go_buy=5000.0, go_sell=5000.0,
)
GEOMETRY = {"min_stop_atr": "1.5", "min_stop_ticks": "0", "stop_beyond_bar": "0", "max_stop_atr": "3"}
PROFILES = {"levels_rr": {"buffer_ticks": 1, "target_R": (1, 2), "shares": (0.5, 0.5), **GEOMETRY}}
LIMITS = RiskLimits(
    per_trade=Decimal("2"), per_instrument=Decimal("6"), per_group={}, portfolio=Decimal("10"),
)
INSTRUMENT = SimpleNamespace(ticker="NGV6", short_name="NG")


def _plan(targets=(("tp-1", Decimal("3.048"), Decimal("0.5")), ("tp-2", Decimal("3.066"), Decimal("0.5")))) -> TradePlan:
    return TradePlan(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NGV6",
        side="BUY",
        signal_id="signal-1",
        reference_entry=Decimal("3.030"),
        stop_price=Decimal("3.012"),
        targets=tuple(TargetPlan(*target) for target in targets),
        profile=ProfileSnapshot("levels_rr", "1", dict(PROFILES["levels_rr"])),
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def _market(**costs) -> dict:
    market = {"price_step": STEP, "entry_cost": Decimal("0"), "exit_cost": Decimal("0"),
              "slippage_cost": Decimal("0")}
    market.update(costs)
    return market


class _Broker:
    """Брокер с метаданными контракта: исполнение в тесте не проверяется."""

    def contract_for(self, ticker: str):
        return META

    def register_trade(self, plan) -> None:
        self.plan = plan


def _manager(tmp_path, **kwargs) -> TradeManager:
    options = {"max_qty": 10}
    options.update(kwargs)
    return TradeManager(
        Storage(tmp_path / "trades.sqlite3"), _Broker(), initial_balance=Decimal("100000"),
        profiles_config=PROFILES, risk_limits=LIMITS, **options,
    )


def _context() -> MarketContext:
    return MarketContext(
        trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
        sr_levels=[SRLevel(Decimal("3.028"), SRType.SUPPORT, 2, "s1")],
        current_price=Decimal("3.030"),
    )


def _admit(manager, *, decision=None, frame_closes=None, instrument=META, management="levels_rr"):
    from src.strategies.contracts import Decision as _Decision

    decision = decision or _Decision(SignalType.BUY, 3.030, event_id="signal-1")
    assignment = SimpleNamespace(id="assignment-1", management=management, priority=1)
    import pandas as pd

    closes = frame_closes or [3.020 + index * 0.0005 for index in range(30)]
    index = pd.date_range("2026-09-01", periods=len(closes), freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {"datetime": index, "open": closes, "high": [c + 0.001 for c in closes],
         "low": [c - 0.001 for c in closes], "close": closes},
    )
    return manager.actions_for_signal(
        assignment, decision, instrument, frame, _context(), timeframe="15m",
    )


def _admit_absolute(manager):
    """Неизменяемые цели 3.033/3.036 дают ровно 378 ₽ gross на Q=10."""
    manager._profiles_config["pattern_targets"] = {"buffer_ticks": 1, "shares": (0.5, 0.5), **GEOMETRY}
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    decision = Decision(SignalType.BUY, 3.030, event_id="signal-1", available_at=now,
                        idea_references={"pattern_id": "test", "c": "3.028", "d": "3.036", "time_available": now})
    return _admit(manager, decision=decision, management="pattern_targets")


def test_risk_and_reward_in_money_for_the_admitted_quantity():
    economics = plan_economics(
        _plan(), quantity=10, price_step=STEP, step_cost=STEP_COST, market=_market(),
    )

    assert economics.quantity == 10
    assert economics.risk_amount == Decimal("1512.00")
    assert economics.reward_amount == Decimal("2268.00")
    assert economics.costs_amount == Decimal("0.00")
    assert economics.payoff_ratio == Decimal("1.50")


def test_costs_reduce_the_payoff_ratio():
    economics = plan_economics(
        _plan(), quantity=10, price_step=STEP, step_cost=STEP_COST,
        market=_market(entry_cost=Decimal("2"), exit_cost=Decimal("2"), slippage_cost=Decimal("2")),
    )

    assert economics.costs_amount == Decimal("60.00")
    assert economics.payoff_ratio < Decimal("1.50")
    assert economics.payoff_ratio > 0


def test_plan_without_targets_claims_no_reward_but_still_prices_its_risk():
    economics = plan_economics(
        _plan(targets=()), quantity=1, price_step=STEP, step_cost=STEP_COST, market=_market(),
    )

    assert economics.risk_amount == Decimal("151.20")
    assert economics.reward_amount == Decimal("0.00")
    assert economics.payoff_ratio is None


def test_economics_require_a_quantity_the_trade_really_has():
    with pytest.raises(ValueError, match="quantity"):
        plan_economics(_plan(), quantity=0, price_step=STEP, step_cost=STEP_COST, market=_market())


def test_economics_survive_a_restart(tmp_path):
    manager = _manager(tmp_path)

    admission = _admit(manager)

    assert admission.plan is not None
    assert admission.plan.economics is not None
    assert admission.plan.stop_basis is not None

    with Storage(tmp_path / "trades.sqlite3") as storage:
        recovered = storage.load_trades()

    assert len(recovered) == 1
    restored = recovered[0].plan.economics
    assert restored == admission.plan.economics
    assert restored.risk_amount == admission.plan.economics.risk_amount
    assert recovered[0].plan.stop_basis == admission.plan.stop_basis


def test_entry_is_refused_when_costs_exceed_the_planned_reward(tmp_path):
    """Позиция, которая заведомо отрицательна вне направления цены, не резервируется."""
    manager = _manager(tmp_path, commission=Decimal("40"), slippage=Decimal("20"),
                       min_risk_cost_ratio=0, max_slippage_r=0)

    admission = _admit_absolute(manager)

    assert admission.actions == ()
    codes = [rejection.code for rejection in admission.rejections]
    assert codes == ["cost-exceeds-reward"]
    assert admission.rejections[0].message == "расходы по сделке превышают плановую доходность"


def test_costs_below_the_reward_do_not_refuse(tmp_path):
    manager = _manager(tmp_path, commission=Decimal("1.5"), slippage=Decimal("1"), min_net_payoff=0)

    admission = _admit(manager)

    assert admission.rejections == ()
    assert len(admission.actions) == 1
    assert admission.plan.economics.costs_amount > 0


def test_costs_are_not_compared_against_a_plan_without_targets(tmp_path):
    """У плана без целей нет планового дохода, и сравнивать с ним нечего."""
    manager = _manager(tmp_path, commission=Decimal("40"), slippage=Decimal("20"),
                       min_risk_cost_ratio=0, max_slippage_r=0)
    manager._profiles_config["ma_cloud"] = {
        "buffer_ticks": 1, "ma_fast_period": 10, "ma_slow_period": 40, **GEOMETRY,
    }
    rising = [3.000 + index * 0.001 for index in range(60)]
    assignment = SimpleNamespace(id="assignment-1", management="ma_cloud", priority=1)

    admission = manager.actions_for_signal(
        assignment, Decision(SignalType.BUY, 3.059, event_id="signal-ma"), META,
        _frame(rising), MarketContext(
            trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
            sr_levels=[], current_price=Decimal("3.059"),
        ), timeframe="15m",
    )

    assert admission.rejections == ()
    assert admission.plan.targets == ()
    assert admission.plan.economics.reward_amount is None
    assert admission.plan.expected_r is None


def test_entry_below_the_minimum_risk_is_refused(tmp_path):
    """Урезанный до неосмысленного риска вход отклоняется, а не проходит молча."""
    manager = _manager(tmp_path, max_qty=1, min_trade_risk_pct=Decimal("2"))

    admission = _admit(manager)

    assert admission.actions == ()
    assert [rejection.code for rejection in admission.rejections] == ["risk-below-floor"]
    assert admission.rejections[0].message == "доступный объём не набирает минимальный риск сделки"


def test_minimum_risk_below_the_admitted_risk_lets_the_entry_through(tmp_path):
    # Один контракт рискует 25.20 ₽, то есть 0.025% баланса: пол в 0.02% выполняется.
    manager = _manager(tmp_path, max_qty=1, min_trade_risk_pct=Decimal("0.02"))

    admission = _admit(manager)

    assert admission.rejections == ()
    assert len(admission.actions) == 1


def test_new_refusals_are_not_disguised_as_budget_problems(tmp_path):
    """Новые коды не должны выглядеть как нехватка бюджета или сбой допуска."""
    refused = _admit_absolute(_manager(tmp_path, commission=Decimal("40"), slippage=Decimal("20")))
    floor_dir = tmp_path / "floor"
    floor_dir.mkdir()
    floor = _admit(_manager(floor_dir, max_qty=1, min_trade_risk_pct=Decimal("2")))

    codes = {
        rejection.code
        for rejection in (*refused.rejections, *floor.rejections)
    }

    assert codes == {"cost-exceeds-reward", "risk-below-floor"}
    assert not codes & {"admission-error", "risk-budget", "margin-budget",
                        "margin-committed-by-pending-orders", "zero-quantity"}


def test_costs_equal_to_the_reward_refuse_the_entry(tmp_path):
    """Расходы, равные доходу, — не профит: равенство уже отказ.

    Доход десяти контрактов 378.00 ₽ (цели 1R/2R по стопу 0.003 от входа 3.030),
    расходы двух сторон при комиссии 18.90 ₽ — тоже 378.00 ₽.
    """
    manager = _manager(tmp_path, commission=Decimal("18.90"), slippage=Decimal("0"))

    admission = _admit_absolute(manager)

    assert admission.actions == ()
    assert [rejection.code for rejection in admission.rejections] == ["cost-exceeds-reward"]
    assert admission.rejections[0].message == "расходы по сделке превышают плановую доходность"


def test_zero_quantity_is_explained_by_the_sizing_budget(tmp_path):
    """Нулевой объём не выдаётся за пол риска: бюджет сайзинга уже нулевой.

    0.0001% от баланса 100 000 ₽ — это 0.10 ₽, тогда как один контракт
    рискует 25.20 ₽: объём не набирается, причина в коде сайзинга.
    """
    tiny = RiskLimits(
        per_trade=Decimal("0.0001"), per_instrument=Decimal("6"), per_group={},
        portfolio=Decimal("0.0001"),
    )
    manager = TradeManager(
        Storage(tmp_path / "trades.sqlite3"), _Broker(), initial_balance=Decimal("100000"),
        profiles_config=PROFILES, risk_limits=tiny, max_qty=10, min_trade_risk_pct=Decimal("2"),
    )

    admission = _admit(manager)

    assert admission.actions == ()
    assert [rejection.code for rejection in admission.rejections] == ["risk-budget"]
    assert "risk-below-floor" not in {rejection.code for rejection in admission.rejections}


def test_positive_risk_below_the_floor_reports_both_numbers(caplog, tmp_path):
    """Положительный риск ниже пола: 25.20 ₽ против пола 2000 ₽ (2% баланса)."""
    import logging

    manager = _manager(tmp_path, max_qty=1, min_trade_risk_pct=Decimal("2"))

    with caplog.at_level(logging.WARNING, logger="src.trade_management.manager"):
        admission = _admit(manager)

    assert [rejection.code for rejection in admission.rejections] == ["risk-below-floor"]
    logged = next(record.getMessage() for record in caplog.records if "risk-below-floor" in record.getMessage())
    assert "quantity=1" in logged
    assert "risk_amount=25.2" in logged
    assert "min_risk=2000" in logged


def test_economics_are_attached_to_the_plan_the_trader_gets(tmp_path):
    admission = _admit(_manager(tmp_path), )

    economics = admission.plan.economics

    assert isinstance(economics, PlanEconomics)
    assert economics.quantity == admission.actions[0].quantity
    assert economics.risk_amount > 0
    assert economics.reward_amount > economics.risk_amount


def _frame(closes=None):
    import pandas as pd

    closes = closes or [3.020 + index * 0.0005 for index in range(30)]
    index = pd.date_range("2026-09-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame(
        {"datetime": index, "open": closes, "high": [c + 0.001 for c in closes],
         "low": [c - 0.001 for c in closes], "close": closes},
    )
