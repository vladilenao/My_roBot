"""Допуск при марже, занятой ACTIVE-резервами незаполненных входов."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.portfolio.models import ContractMeta
from src.portfolio.risk import RiskLimits
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.errors import ReservationRejected
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

BUDGET = Decimal("99380.72")
GO = 13540.45
COMMITTED_MARGIN = Decimal("94783")
META = ContractMeta(ticker="NGV6", price_step=1.0, step_cost=100.0, go_buy=GO, go_sell=GO)
INSTRUMENT = SimpleNamespace(ticker="NGV6", short_name="NG")

PROFILES = {"levels_rr": {"buffer_ticks": 1, "target_R": (1, 2), "shares": (0.5, 0.5)}}
WIDE_LIMITS = RiskLimits(
    per_trade=Decimal("100"), per_instrument=Decimal("100"),
    per_group={}, portfolio=Decimal("100"),
)


class _Broker:
    """Брокер с метаданными контракта; регистрирует план, но не исполняет."""

    def __init__(self, meta: ContractMeta | None = META) -> None:
        self._meta = meta
        self.registered: list[TradePlan] = []

    def register_trade(self, plan: TradePlan) -> None:
        self.registered.append(plan)

    def contract_for(self, ticker: str) -> ContractMeta | None:
        return self._meta


def _frame(closes, start="2026-01-01 09:00"):
    index = pd.date_range(start, periods=len(closes), freq="min", tz="UTC")
    return pd.DataFrame(
        {"datetime": index, "open": closes, "high": [c + 1.0 for c in closes],
         "low": [c - 1.0 for c in closes], "close": closes}
    )


def _context(*, price=100.0, levels=()):
    return MarketContext(
        trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
        sr_levels=list(levels),
        current_price=price,
    )


def _level_context():
    return _context(price=100.0, levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),))


def _assignment(identifier="assignment-1"):
    return SimpleNamespace(id=identifier, strategy="macd_rsi_stoch",
                           management="levels_rr", filter_profile="basic_levels",
                           priority=0, timeframe="15m")


def _decision(event_id="signal-1"):
    return Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                    event_id=event_id, available_at=BAR0, timeframe="15m")


def _manager(storage, *, broker=None, balance=BUDGET, limits=WIDE_LIMITS, max_qty=7):
    return TradeManager(
        storage, broker or _Broker(), initial_balance=balance,
        profiles_config=PROFILES, risk_limits=limits, max_qty=max_qty,
    )


def _seed_pending_entry(
    manager,
    *,
    trade_id="seed-trade",
    assignment_id="seed-assignment",
    risk=Decimal("1"),
    margin=COMMITTED_MARGIN,
    risk_budget=Decimal("100000"),
    margin_budget=Decimal("100000"),
    quantity=7,
):
    """Оставить ACTIVE-резерв незаполненного входа, как после первого сигнала."""
    plan = TradePlan(
        trade_id, assignment_id, "NGV6", "BUY", f"signal:{trade_id}",
        Decimal("100"), Decimal("96"),
        (TargetPlan(f"tp:{trade_id}", Decimal("104"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
    )
    command_id = f"{trade_id}:entry"
    reservation = ReservationCandidate(
        reservation_id=f"reservation:{trade_id}", trade_id=trade_id, order_id=command_id,
        priority=0, assignment_id=assignment_id, instrument_id="NGV6",
        signal_id=f"signal:{trade_id}", risk_amount=risk, margin_amount=margin,
    )
    accepted = manager.submit_plan(
        plan, OpenTrade(command_id, trade_id, 0, "profile-entry", quantity),
        reservation=reservation, risk_budget=risk_budget, margin_budget=margin_budget,
    )
    assert accepted


def _active_reservations(storage):
    return storage.connection.execute(
        "SELECT risk_amount, margin_amount FROM reservations WHERE status = 'ACTIVE'"
    ).fetchall()


def test_second_entry_rejected_when_margin_committed_by_pending_order(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, limits=RiskLimits(
            per_trade=Decimal("2"), per_instrument=Decimal("100"),
            per_group={}, portfolio=Decimal("100"),
        ))
        _seed_pending_entry(manager)

        admission = manager.actions_for_signal(
            _assignment("assignment-2"), _decision("signal-2"), INSTRUMENT,
            _frame([100.0] * 25), _level_context(), timeframe="15m",
        )

        assert len(admission) == 0
        assert [reason.code for reason in admission.rejections] == [
            "margin-committed-by-pending-orders"
        ]
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM trades WHERE assignment_id = 'assignment-2'"
        ).fetchone()[0] == 0


def test_margin_budget_code_without_active_reserves(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, balance=Decimal("10000"), max_qty=7)
        admission = manager.actions_for_signal(
            _assignment(), _decision(), INSTRUMENT,
            _frame([100.0] * 25), _level_context(), timeframe="15m",
        )

        assert len(admission) == 0
        assert [reason.code for reason in admission.rejections] == ["margin-budget"]


def test_risk_budget_code_when_risk_limit_is_exhausted(tmp_path):
    zero_limits = RiskLimits(
        per_trade=Decimal("0"), per_instrument=Decimal("0"),
        per_group={}, portfolio=Decimal("0"),
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, limits=zero_limits)
        admission = manager.actions_for_signal(
            _assignment(), _decision(), INSTRUMENT,
            _frame([100.0] * 25), _level_context(), timeframe="15m",
        )

        assert len(admission) == 0
        assert [reason.code for reason in admission.rejections] == ["risk-budget"]


def test_sizing_does_not_propose_volume_reservation_would_reject_by_margin(tmp_path):
    """Боевой случай: резерв 94 783 ₽ при бюджете 99 380,72 ₽, второй вход 7 контрактов."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, limits=RiskLimits(
            per_trade=Decimal("2"), per_instrument=Decimal("100"),
            per_group={}, portfolio=Decimal("100"),
        ), max_qty=7)
        _seed_pending_entry(manager, risk=Decimal("0"))

        plan = TradePlan(
            "second-trade", "assignment-2", "NGV6", "BUY", "signal-2",
            Decimal("100"), Decimal("97.2"),
            (TargetPlan("tp-2", Decimal("104"), Decimal("1")),),
            ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
        )
        sizing = manager._size_open_quantity(plan, META)

        assert sizing.code == "margin-committed-by-pending-orders"
        assert sizing.quantity == 0
        assert sizing.candidate_quantity == 7
        free_margin = BUDGET - COMMITTED_MARGIN
        assert Decimal(str(GO)) * sizing.candidate_quantity > free_margin


@pytest.mark.parametrize(
    "setup,code",
    [
        ("margin-committed", "margin-committed-by-pending-orders"),
        ("margin-budget", "margin-budget"),
        ("risk-budget", "risk-budget"),
    ],
)
def test_rejection_numbers_are_logged(tmp_path, caplog, setup, code):
    limits = RiskLimits(
        per_trade=Decimal("2"), per_instrument=Decimal("100"),
        per_group={}, portfolio=Decimal("100"),
    )
    balance = BUDGET
    seed = None
    if setup == "margin-budget":
        balance = Decimal("10000")
        limits = WIDE_LIMITS
    elif setup == "risk-budget":
        limits = RiskLimits(
            per_trade=Decimal("0"), per_instrument=Decimal("0"),
            per_group={}, portfolio=Decimal("0"),
        )
    elif setup == "margin-committed":
        seed = True

    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, balance=balance, limits=limits)
        if seed:
            _seed_pending_entry(manager)
        with caplog.at_level(logging.INFO, logger="src.trade_management.manager"):
            manager.actions_for_signal(
                _assignment("assignment-2"), _decision("signal-2"), INSTRUMENT,
                _frame([100.0] * 25), _level_context(), timeframe="15m",
            )

    records = [record for record in caplog.records if record.name == "src.trade_management.manager"]
    assert any(
        code in record.getMessage()
        and "used_risk=" in record.getMessage()
        and "candidate_risk=" in record.getMessage()
        and "risk_budget=" in record.getMessage()
        and "used_margin=" in record.getMessage()
        and "candidate_margin=" in record.getMessage()
        and "margin_budget=" in record.getMessage()
        for record in records
    )


def test_single_entry_without_competition_keeps_its_volume(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, balance=Decimal("100000"), limits=RiskLimits(
            per_trade=Decimal("2"), per_instrument=Decimal("100"),
            per_group={}, portfolio=Decimal("100"),
        ), max_qty=4)
        admission = manager.actions_for_signal(
            _assignment(), _decision(), INSTRUMENT,
            _frame([100.0] * 25), _level_context(), timeframe="15m",
        )

        assert len(admission) == 1
        assert admission[0].quantity == 4
        assert not admission.rejections


def test_reserve_distinguishes_margin_committed_from_general_budget(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage)
        _seed_pending_entry(manager, risk=Decimal("0"), margin=COMMITTED_MARGIN,
                            risk_budget=Decimal("100000"), margin_budget=Decimal("100000"))

        plan = TradePlan(
            "second-trade", "assignment-2", "NGV6", "BUY", "signal-2",
            Decimal("100"), Decimal("96"),
            (TargetPlan("tp-2", Decimal("104"), Decimal("1")),),
            ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
        )
        command_id = "second-trade:entry"
        reservation = ReservationCandidate(
            reservation_id="reservation:second-trade", trade_id="second-trade",
            order_id=command_id, priority=0, assignment_id="assignment-2",
            instrument_id="NGV6", signal_id="signal-2",
            risk_amount=Decimal("10"), margin_amount=Decimal("27080.90"),
        )
        with pytest.raises(ReservationRejected) as raised:
            manager.submit_plan(
                plan, OpenTrade(command_id, "second-trade", 0, "profile-entry", 2),
                reservation=reservation, risk_budget=Decimal("100000"),
                margin_budget=COMMITTED_MARGIN,
            )

        assert raised.value.code == "margin-committed-by-pending-orders"
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_id = 'second-trade'"
        ).fetchone()[0] == 0


def test_reserve_reports_general_margin_budget_without_reserves(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage)
        plan = TradePlan(
            "solo-trade", "assignment-1", "NGV6", "BUY", "signal-1",
            Decimal("100"), Decimal("96"),
            (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
            ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
        )
        command_id = "solo-trade:entry"
        reservation = ReservationCandidate(
            reservation_id="reservation:solo-trade", trade_id="solo-trade",
            order_id=command_id, priority=0, assignment_id="assignment-1",
            instrument_id="NGV6", signal_id="signal-1",
            risk_amount=Decimal("10"), margin_amount=Decimal("500"),
        )
        with pytest.raises(ReservationRejected) as raised:
            manager.submit_plan(
                plan, OpenTrade(command_id, "solo-trade", 0, "profile-entry", 2),
                reservation=reservation, risk_budget=Decimal("100000"),
                margin_budget=Decimal("100"),
            )

        assert raised.value.code == "margin-budget"


def test_reserve_reports_risk_budget(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage)
        plan = TradePlan(
            "solo-trade", "assignment-1", "NGV6", "BUY", "signal-1",
            Decimal("100"), Decimal("96"),
            (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
            ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
        )
        command_id = "solo-trade:entry"
        reservation = ReservationCandidate(
            reservation_id="reservation:solo-trade", trade_id="solo-trade",
            order_id=command_id, priority=0, assignment_id="assignment-1",
            instrument_id="NGV6", signal_id="signal-1",
            risk_amount=Decimal("500"), margin_amount=Decimal("500"),
        )
        with pytest.raises(ReservationRejected) as raised:
            manager.submit_plan(
                plan, OpenTrade(command_id, "solo-trade", 0, "profile-entry", 2),
                reservation=reservation, risk_budget=Decimal("100"),
                margin_budget=Decimal("100000"),
            )

        assert raised.value.code == "risk-budget"
