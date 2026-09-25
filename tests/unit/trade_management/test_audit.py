from datetime import datetime, timezone
from decimal import Decimal
import json
import logging

import pytest

from src.trade_journal.storage import Storage
from src.trade_journal.storage import ReservationCandidate
from src.trade_journal.reducer import AccountState, PositionState, apply_fill_with_trace
from src.trade_management.audit import (
    CalculationTrace,
    CalculationTraceRepository,
    FormulaStep,
    MarketInput,
    MeasuredValue,
    Rounding,
    TraceLinks,
    TraceOutcome,
    calculation_trace,
)
from src.trade_management.models import ProfileSnapshot, TradePlan
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.base import PlanningContext
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.ma_cloud import MaCloudProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile
from src.strategies.contracts import Decision, SignalType
from src.decision.filters.basic_levels import BasicLevelsFilter
from src.market_context.models import MarketContext, TrendDirection, TrendResult
from src.portfolio.risk import PortfolioRiskManager, RiskAddition, RiskLimits, RiskTrade
from src.strategies.indicators.atr.indicator import AtrWilderIndicator
from src.strategies.indicators.ma.indicator import MaCloudIndicator
import pandas as pd


UTC = timezone.utc
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _input(source_data_id="bars-1", close="100.125"):
    return MarketInput(
        source_data_id=source_data_id,
        kind="closed-ohlc-window",
        values={"close": MeasuredValue(Decimal(close), "price")},
        seed={"atr_seed": MeasuredValue(Decimal("2.5"), "price")},
        available_at=NOW,
        created_at=NOW,
    )


def _trace(calculation_id, outcome, reason, source_data_id="bars-1"):
    raw = MeasuredValue(Decimal("100.125"), "price")
    rounded = MeasuredValue(Decimal("100.00"), "price")
    return CalculationTrace(
        calculation_id=calculation_id,
        algorithm="position-sizing",
        algorithm_version="1",
        source_data_id=source_data_id,
        inputs={
            "entry": raw,
            "price_step": MeasuredValue(Decimal("0.125"), "price"),
            "risk_budget": MeasuredValue(Decimal("1000"), "RUB"),
        },
        steps=(FormulaStep("floor(risk_budget / per_contract_risk)", {
            "risk_budget": MeasuredValue(Decimal("1000"), "RUB"),
            "per_contract_risk": MeasuredValue(Decimal("420"), "RUB/contracts"),
        }, MeasuredValue(2, "contracts")),),
        rounding=(Rounding("floor to price step", raw, rounded),),
        result=MeasuredValue(2 if outcome is TraceOutcome.ACCEPTED else 0, "contracts"),
        outcome=outcome,
        reason=reason,
        links=TraceLinks(
            assignment_id="assignment-1", signal_id="signal-1", service_uid="bot-1",
            correlation_id="tick-1",
        ),
        created_at=NOW,
    )


def test_successful_trace_persists_complete_reproducible_logging_contract(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trace = _trace("calculation-success", TraceOutcome.ACCEPTED, "risk-within-limit")
        assert CalculationTraceRepository(storage).record(trace, _input())

        row = storage.connection.execute("SELECT * FROM calculations").fetchone()
        columns = [item[0] for item in storage.connection.execute("SELECT * FROM calculations").description]
        persisted = dict(zip(columns, row, strict=True))
        market = storage.connection.execute("SELECT * FROM market_inputs").fetchone()

    assert persisted["outcome"] == "ACCEPTED"
    assert persisted["reason"] == "risk-within-limit"
    assert persisted["assignment_id"] == "assignment-1"
    assert persisted["signal_id"] == "signal-1"
    assert persisted["service_uid"] == "bot-1"
    assert persisted["correlation_id"] == "tick-1"
    assert json.loads(persisted["input_json"])["price_step"]["unit"] == "price"
    assert json.loads(persisted["steps_json"])[0]["formula"] == "floor(risk_budget / per_contract_risk)"
    assert json.loads(persisted["rounding_json"])[0]["rounded"]["value"] == "100.00"
    assert json.loads(persisted["output_json"]) == {"unit": "contracts", "value": 2}
    assert json.loads(market[2])["close"]["unit"] == "price"
    assert json.loads(market[3])["atr_seed"]["value"] == "2.5"


def test_rejected_trace_keeps_formula_units_rounding_reason_and_links(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        trace = _trace("calculation-rejected", TraceOutcome.REJECTED, "risk-limit-exhausted")
        assert CalculationTraceRepository(storage).record(trace, _input())
        row = storage.connection.execute(
            "SELECT outcome, reason, input_json, steps_json, rounding_json, output_json, "
            "assignment_id, signal_id FROM calculations"
        ).fetchone()

    outcome, reason, inputs, steps, rounding, result, assignment_id, signal_id = row
    assert (outcome, reason, assignment_id, signal_id) == (
        "REJECTED", "risk-limit-exhausted", "assignment-1", "signal-1",
    )
    assert json.loads(inputs)["risk_budget"]["unit"] == "RUB"
    assert json.loads(steps)[0]["result"] == {"unit": "contracts", "value": 2}
    assert json.loads(rounding)[0]["unrounded"]["value"] == "100.125"
    assert json.loads(result) == {"unit": "contracts", "value": 0}


def test_market_input_is_immutable_and_cannot_be_replaced_for_same_source(tmp_path):
    market_input = _input(close="100")
    with pytest.raises(TypeError):
        market_input.values["close"] = MeasuredValue(Decimal("200"), "price")

    with Storage(tmp_path / "trades.sqlite3") as storage:
        repository = CalculationTraceRepository(storage)
        repository.record(_trace("calculation-1", TraceOutcome.ACCEPTED, "accepted"), market_input)
        with pytest.raises(ValueError, match="immutable market input"):
            repository.record(
                _trace("calculation-2", TraceOutcome.ACCEPTED, "accepted"),
                _input(close="101"),
            )


def test_trace_exports_structured_values_and_all_identifiers_independent_of_root_level(tmp_path, monkeypatch):
    audit_path = tmp_path / "trade_decision_trace.log"
    monkeypatch.setattr(logging.getLogger(), "level", logging.CRITICAL)
    trace = _trace("calculation-audit", TraceOutcome.ACCEPTED, "risk-within-limit")
    trace = CalculationTrace(
        **{**trace.__dict__, "links": TraceLinks(
            assignment_id="assignment-1",
            trade_id="trade-1",
            signal_id="signal-1",
            command_id="command-1",
            event_id="event-1",
            service_uid="bot-1",
            correlation_id="tick-1",
        )}
    )

    with Storage(tmp_path / "trades.sqlite3", audit_path=audit_path) as storage:
        timestamp = NOW.isoformat()
        with storage.transaction() as connection:
            connection.execute(
                "INSERT INTO trades VALUES ('trade-1', 'assignment-1', 'NGV6', 'signal-1', 'BUY', "
                "'{}', '{}', 'OPEN', 0, '{}', ?, ?, NULL, NULL)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO outbox VALUES ('command-1', 'trade-1', '{}', 'PENDING', ?, NULL)",
                (timestamp,),
            )
            connection.execute(
                "INSERT INTO events (event_id, trade_id, command_id, event_type, payload_json, occurred_at) "
                "VALUES ('event-1', 'trade-1', 'command-1', 'TRACE', '{}', ?)",
                (timestamp,),
            )
        assert CalculationTraceRepository(storage).record(trace, _input())

    exported = json.loads(audit_path.read_text(encoding="utf-8"))
    assert exported["calculation_id"] == "calculation-audit"
    assert exported["trade_id"] == "trade-1"
    assert exported["command_id"] == "command-1"
    assert exported["event_id"] == "event-1"
    assert exported["assignment_id"] == "assignment-1"
    assert exported["signal_id"] == "signal-1"
    assert exported["service_uid"] == "bot-1"
    assert exported["correlation_id"] == "tick-1"
    assert exported["inputs"]["price_step"] == {"unit": "price", "value": "0.125"}
    assert exported["steps"][0]["result"] == {"unit": "contracts", "value": 2}
    assert exported["rounding"][0]["rounded"] == {"unit": "price", "value": "100.00"}


def test_trace_id_is_deterministic_and_contains_only_supplied_local_inputs():
    inputs = {"entry": MeasuredValue(Decimal("100"), "price")}
    first = calculation_trace("signal.example", inputs=inputs, result=MeasuredValue("BUY", "signal"),
                              reason="confirmed", formula="local rule")
    second = calculation_trace("signal.example", inputs=inputs, result=MeasuredValue("BUY", "signal"),
                               reason="confirmed", formula="local rule")

    assert first.calculation_id == second.calculation_id
    assert first.steps[0].formula == "local rule"
    assert first.inputs["entry"].unit == "price"


def test_filter_trace_records_signal_trend_and_rejection_reason():
    decision, trace = BasicLevelsFilter().apply_with_trace(
        Decision(SignalType.BUY, 100.0, event_id="signal-1"),
        MarketContext(TrendResult(TrendDirection.DOWN, 0.8), [], 100.0), "NG", "15m",
    )

    assert decision.signal_type is SignalType.HOLD
    assert trace.outcome is TraceOutcome.REJECTED
    assert trace.reason == "trend-opposes-entry"
    assert trace.inputs["trend"] == MeasuredValue("down", "trend-direction")
    assert trace.inputs["timeframe"] == MeasuredValue("15m", "timeframe")


@pytest.mark.parametrize(("profile", "snapshot", "market", "references"), [
    (LevelsRrProfile(), ProfileSnapshot("levels_rr", "1", {"target_R": (1, 2), "shares": ("0.5", "0.5")}),
     {"price_step": "1", "support": "97"}, None),
    (AtrTrendProfile(), ProfileSnapshot("atr_trend", "1", {}), {"price_step": "1", "atr": "2"}, None),
    (MaCloudProfile(), ProfileSnapshot("ma_cloud", "1", {}), {"price_step": "1", "ma10": "99", "ma40": "98"}, None),
    (PatternTargetsProfile(), ProfileSnapshot("pattern_targets", "1", {"shares": ("0.5", "0.5")} ),
     {"price_step": "1"}, {"pattern_id": "p-1", "c": "96", "d": "110", "time_available": NOW}),
])
def test_all_profiles_emit_reproducible_plan_trace(profile, snapshot, market, references):
    signal = Decision(SignalType.BUY, 100.0, event_id="signal-1", available_at=NOW,
                      idea_references=references)
    result, trace = profile.plan_with_trace(PlanningContext(
        "trade-1", "assignment-1", "NG-10.26", signal, snapshot, market,
    ))

    assert isinstance(result, TradePlan)
    assert trace.algorithm == f"profile.{profile.NAME}.plan"
    assert trace.inputs["market"].unit == "local-market-inputs"
    assert trace.inputs["parameters"].unit == "profile-parameters"
    assert trace.result.value["stop"] == str(result.stop_price)
    assert trace.links.trade_id == "trade-1"


def test_sizing_reservation_and_clearing_traces_include_independent_inputs(tmp_path):
    trade = RiskTrade("trade-1", "NG", frozenset(), "BUY", 1, Decimal("100"), Decimal("96"),
                      Decimal("1"), Decimal("100"))
    quantity, sizing = PortfolioRiskManager().maximum_additional_quantity_with_trace(
        balance=Decimal("1000"), equity=Decimal("1000"),
        limits=RiskLimits(Decimal("100"), Decimal("100"), {}, Decimal("100")), trades=(trade,),
        addition=RiskAddition("trade-1", Decimal("100"), 3, Decimal("20")),
    )
    assert quantity == 1
    assert sizing.inputs["step_cost"].unit == "RUB/tick"
    assert sizing.inputs["entry_fee"].value == Decimal("20")

    with Storage(tmp_path / "trades.sqlite3") as storage:
        timestamp = NOW.isoformat()
        with storage.transaction() as connection:
            connection.execute(
                "INSERT INTO trades VALUES ('trade-1', 'assignment-1', 'NG', 'signal-1', 'BUY', "
                "'{}', '{}', 'OPEN', 0, '{}', ?, ?, NULL, NULL)", (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO outbox VALUES ('command-1', 'trade-1', '{}', 'PENDING', ?, NULL)",
                (timestamp,),
            )
            connection.execute(
                "INSERT INTO orders VALUES ('command-1', 'trade-1', 'command-1', 'OPEN', 'PENDING', "
                "1, 0, NULL, ?, ?)", (timestamp, timestamp),
            )
        decisions, traces = storage.reserve_candidates_with_traces((ReservationCandidate(
            "reservation-1", "trade-1", "command-1", 1, "assignment-1", "NG", "signal-1",
            Decimal("400"), Decimal("100"),
        ),), risk_budget=Decimal("500"), margin_budget=Decimal("500"), created_at=NOW)
    assert decisions[0].accepted is True
    assert traces[0].inputs["candidate_risk"] == MeasuredValue(Decimal("400"), "RUB")
    assert traces[0].links.command_id == "command-1"

    _, account, clearing = apply_fill_with_trace(
        PositionState(1, Decimal("100"), Decimal("0"), Decimal("2")),
        AccountState(Decimal("998"), Decimal("998"), Decimal("0"), Decimal("2")),
        side="BUY", action_type="CLOSE", quantity=1, price=Decimal("105"), fee=Decimal("1"),
    )
    assert account.net_realized_pnl == Decimal("2")
    assert clearing.inputs["fee"].unit == "RUB"
    assert clearing.result.value["gross_pnl"] == "5"


def test_indicator_traces_retain_closed_windows_and_recurrent_seed_inputs():
    candles = pd.DataFrame({"high": [101, 102, 103], "low": [99, 100, 101], "close": [100, 101, 102]})
    _, atr = AtrWilderIndicator(period=2).compute_with_trace(candles)
    _, ma = MaCloudIndicator(fast_period=2, slow_period=3).compute_with_trace(candles)

    assert atr.inputs["closed_ohlc"].unit == "OHLC"
    assert atr.inputs["true_ranges"].value == [2, 2]
    assert "Wilder recurrence" in atr.steps[0].formula
    assert ma.inputs["closed_closes"].value == [100, 101, 102]
    assert ma.result.value == {"sma_fast": 101.5, "sma_slow": 101.0}
