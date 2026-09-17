"""Reproducible audit example for the user docs: accepted entry and denied add.

These are the two traces shown in `docs/trade-management/storage-and-audit.md`.
Each test exercises the real modules and asserts the audited facts, so a doc
reader can regenerate the JSONL lines with a one-off script.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from src.portfolio.risk import PortfolioRiskManager, RiskAddition, RiskLimits, RiskTrade
from src.strategies.contracts import Decision, SignalType
from src.trade_management.audit import TraceOutcome
from src.trade_management.models import ProfileSnapshot
from src.trade_management.profiles.base import PlanningContext
from src.trade_management.profiles.levels_rr import LevelsRrProfile


def _trace_fixture() -> tuple[str, str, str]:
    return f"trade-{uuid4().hex[:8]}", f"assign-{uuid4().hex[:8]}", f"sig-{uuid4().hex[:8]}"


def _levels_rr_context(trade_id: str, assignment_id: str, instrument_id: str, signal_id: str) -> PlanningContext:
    signal = Decision(
        event_id=signal_id,
        signal_type=SignalType.BUY,
        price=100.0,
        available_at=datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc),
        timeframe="1h",
        strategy_name="levels_rr",
        indicator_values={"action": "entry", "entry_price_level": "high"},
    )
    profile = ProfileSnapshot(
        name="levels_rr",
        version="1",
        parameters={
            "buffer_ticks": 1,
            "target_R": [Decimal("1"), Decimal("2")],
            "shares": [Decimal("0.5"), Decimal("0.5")],
            "max_adds": 0,
        },
    )
    return PlanningContext(
        trade_id=trade_id,
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        signal=signal,
        profile=profile,
        market={
            "support": Decimal("97"),
            "resistance": Decimal("103"),
            "price_step": Decimal("1"),
            "entry": Decimal("100"),
        },
    )


def test_docs_entry_audit_trace_is_accepted_with_doc_values() -> None:
    """The accepted-entry trace repeats the numbers from profiles.md 96/104/108."""
    trade_id, assignment_id, signal_id = _trace_fixture()
    instrument_id = "NG"
    _, trace = LevelsRrProfile().plan_with_trace(
        _levels_rr_context(trade_id, assignment_id, instrument_id, signal_id)
    )

    assert trace.algorithm == "profile.levels_rr.plan"
    assert trace.outcome is TraceOutcome.ACCEPTED
    assert trace.reason == "planned"
    result = trace.result.value
    assert result["entry"] == "100.0"
    assert result["stop"] == "96"
    assert result["targets"] == [("tp-1", "104", "0.5"), ("tp-2", "108", "0.5")]
    assert trace.links.assignment_id == assignment_id
    assert trace.links.trade_id == trade_id
    assert trace.links.signal_id is not None


def test_docs_denied_add_audit_trace_is_rejected_with_zero_quantity() -> None:
    """An add beyond the portfolio budget is audited as a REJECTED sizing trace."""
    trade_id, _assignment_id, _signal_id = _trace_fixture()
    trade = RiskTrade(
        trade_id=trade_id,
        instrument_id="NG",
        groups=frozenset(),
        side="BUY",
        quantity=2,
        average_price=Decimal("100"),
        stop_price=Decimal("96"),
        price_step=Decimal("1"),
        step_cost=Decimal("100"),
        slippage_allowance=Decimal("20"),
    )
    addition = RiskAddition(
        trade_id=trade_id,
        entry_price=Decimal("100"),
        max_total_quantity=3,
        slippage_allowance_per_contract=Decimal("20"),
    )
    manager = PortfolioRiskManager()
    quantity, trace = manager.maximum_additional_quantity_with_trace(
        balance=Decimal("1000"),
        equity=Decimal("1000"),
        limits=RiskLimits(
            per_trade=Decimal("100"),
            per_instrument=Decimal("100"),
            per_group={},
            portfolio=Decimal("100"),
        ),
        trades=(trade,),
        addition=addition,
    )

    assert quantity == 0
    assert trace.algorithm == "portfolio.maximum_additional_quantity"
    assert trace.outcome is TraceOutcome.REJECTED
    assert trace.result.value == 0
    assert trace.result.unit == "contracts"
    assert trace.reason == "largest-permitted-integer-quantity"
    assert trace.inputs["budget_base"].value == Decimal("1000")
    assert trace.inputs["stop_price"].value == Decimal("96")
    assert trace.inputs["slippage"].value == Decimal("20")
    assert trace.links.trade_id == trade_id


def test_docs_rejected_add_nowhere_near_budget_is_rejected_without_other_limits() -> None:
    """Sanity: the same sizing succeeds when the budget fits, guarding the doc claim."""
    trade_id, _assignment_id, _signal_id = _trace_fixture()
    trade = RiskTrade(
        trade_id=trade_id,
        instrument_id="NG",
        groups=frozenset(),
        side="BUY",
        quantity=1,
        average_price=Decimal("100"),
        stop_price=Decimal("96"),
        price_step=Decimal("1"),
        step_cost=Decimal("100"),
        slippage_allowance=Decimal("20"),
    )
    addition = RiskAddition(
        trade_id=trade_id,
        entry_price=Decimal("100"),
        max_total_quantity=2,
        slippage_allowance_per_contract=Decimal("20"),
    )
    quantity, trace = PortfolioRiskManager().maximum_additional_quantity_with_trace(
        balance=Decimal("4200"),
        equity=Decimal("4200"),
        limits=RiskLimits(
            per_trade=Decimal("100"),
            per_instrument=Decimal("100"),
            per_group={},
            portfolio=Decimal("100"),
        ),
        trades=(trade,),
        addition=addition,
    )

    assert quantity == 1
    assert trace.outcome is TraceOutcome.ACCEPTED
    assert trace.result.value == 1