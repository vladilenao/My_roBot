"""Общая геометрия стопа на уровне профилей: расширение, сокращение, R-цели.

Профили не знают про волатильность инструмента, поэтому проверяется именно то,
что их планы проходят через общее правило, а отказы остаются их собственными.
"""

from decimal import Decimal

import pandas as pd

from src.strategies.contracts import Decision, SignalType
from src.trade_management.audit import TraceOutcome
from src.trade_management.models import ProfileSnapshot, StopBasis
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.base import PlanningContext, ProfileResult
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.ma_cloud import MaCloudProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile

# Геометрия проверяется здесь, поэтому в тестах профилей она закреплена явно.
GEOMETRY = {"min_stop_atr": "1.5", "min_stop_ticks": "0", "stop_beyond_bar": "0", "max_stop_atr": "3"}


def _context(
    profile: str,
    parameters: dict,
    market: dict,
    *,
    side=SignalType.BUY,
    price="3.030",
    references: dict | None = None,
) -> PlanningContext:
    return PlanningContext(
        trade_id="trade-1",
        assignment_id="assignment-1",
        instrument_id="NGV6",
        signal=Decision(
            side, float(price), event_id="signal-1", idea_references=references,
            available_at=pd.Timestamp("2026-09-01T10:00:00") if references else None,
        ),
        profile=ProfileSnapshot(profile, "1", {**parameters, **GEOMETRY}),
        market=market,
    )


def _levels_context(**overrides) -> PlanningContext:
    return _context("levels_rr", {"buffer_ticks": 1, "target_R": (1, 2), "shares": ("0.5", "0.5")},
                    {"price_step": "0.001", "support": "3.028", **overrides})


def test_levels_rr_stop_inside_the_noise_is_widened_and_targets_move_with_it():
    plan = LevelsRrProfile().plan(_levels_context(atr="0.012"))

    assert plan.stop_price == Decimal("3.012")
    assert plan.risk_per_unit == Decimal("0.018")
    assert plan.stop_basis is StopBasis.ATR_FLOOR
    assert tuple(target.price for target in plan.targets) == (Decimal("3.048"), Decimal("3.066"))
    assert tuple(target.share for target in plan.targets) == (Decimal("0.5"), Decimal("0.5"))


def test_levels_rr_uses_the_atr_of_the_instrument_it_was_given():
    plan = LevelsRrProfile().plan(_levels_context(atr="0.004"))

    assert plan.stop_price == Decimal("3.024")
    assert plan.stop_basis is StopBasis.ATR_FLOOR
    assert plan.risk_per_unit == Decimal("0.006")


def test_levels_rr_structural_stop_survives_inside_the_bounds():
    context = _context(
        "levels_rr", {"buffer_ticks": 1, "target_R": (1, 2), "shares": ("0.5", "0.5")},
        {"price_step": "0.001", "support": "3.028", "atr": "0.030"}, price="3.100",
    )

    plan = LevelsRrProfile().plan(context)

    assert plan.stop_price == Decimal("3.027")
    assert plan.risk_per_unit == Decimal("0.073")
    assert plan.stop_basis is StopBasis.STRUCTURAL
    assert tuple(target.price for target in plan.targets) == (Decimal("3.173"), Decimal("3.246"))


def test_levels_rr_far_level_is_shortened_to_the_cap():
    context = _context(
        "levels_rr", {"buffer_ticks": 1, "target_R": (1, 2), "shares": ("0.5", "0.5")},
        {"price_step": "0.001", "support": "2.800", "atr": "0.030"}, price="3.030",
    )

    plan = LevelsRrProfile().plan(context)

    assert plan.stop_price == Decimal("2.940")
    assert plan.stop_basis is StopBasis.ATR_CAP
    assert tuple(target.price for target in plan.targets) == (Decimal("3.120"), Decimal("3.210"))


def test_levels_rr_without_structure_keeps_its_own_refusal():
    result = LevelsRrProfile().plan(_levels_context(atr="0.012", support=None))

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "missing-structure"}


def test_atr_trend_stop_below_the_floor_is_lifted_to_it():
    context = _context(
        "atr_trend", {"atr_period": 14, "initial_k": "0.4", "trail_k": 2, "tp1_R": 1, "tp1_share": "0.5"},
        {"price_step": "0.001", "atr": "0.010"},
    )

    plan = AtrTrendProfile().plan(context)

    assert plan.stop_price == Decimal("3.015")
    assert plan.risk_per_unit == Decimal("0.015")
    assert plan.stop_basis is StopBasis.ATR_FLOOR
    assert tuple(target.price for target in plan.targets) == (Decimal("3.045"),)


def test_atr_trend_without_atr_keeps_its_own_refusal():
    context = _context(
        "atr_trend", {"atr_period": 14, "initial_k": "0.4", "trail_k": 2, "tp1_R": 1, "tp1_share": "0.5"},
        {"price_step": "0.001", "atr": None},
    )

    result = AtrTrendProfile().plan(context)

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "insufficient-history"}


def test_pattern_targets_keep_absolute_prices_when_the_stop_is_widened():
    context = _context(
        "pattern_targets", {"buffer_ticks": 1, "fractions_to_D": (0.5, 1.0), "shares": ("0.5", "0.5")},
        {"price_step": "0.001", "atr": "0.012"},
        price="3.030",
        references={
            "pattern_id": "long", "c": "3.028", "d": "3.080",
            "time_available": "2026-09-01T10:00:00",
        },
    )

    plan = PatternTargetsProfile().plan(context)

    assert plan.stop_price == Decimal("3.012")
    assert plan.stop_basis is StopBasis.ATR_FLOOR
    # Мидпоинт и D заданы абсолютными ценами: расширенный стоп их не двигает.
    assert tuple(target.price for target in plan.targets) == (Decimal("3.055"), Decimal("3.080"))


def test_ma_cloud_stop_is_bounded_but_the_plan_keeps_no_targets():
    context = _context(
        "ma_cloud", {"buffer_ticks": 1, "ma_fast_period": 10, "ma_slow_period": 40},
        {"price_step": "0.001", "ma10": "3.029", "ma40": "3.020", "atr": "0.012"},
    )

    plan = MaCloudProfile().plan(context)

    assert plan.stop_price == Decimal("3.012")
    assert plan.stop_basis is StopBasis.ATR_FLOOR
    assert plan.targets == ()
    assert plan.expected_r is None


def test_ma_cloud_without_a_cloud_keeps_its_own_refusal():
    context = _context(
        "ma_cloud", {"buffer_ticks": 1, "ma_fast_period": 10, "ma_slow_period": 40},
        {"price_step": "0.001", "ma10": None, "ma40": None, "atr": "0.012"},
    )

    result = MaCloudProfile().plan(context)

    assert isinstance(result, ProfileResult)
    assert result.state == {"reason": "insufficient-history"}


def test_plan_trace_carries_the_stop_basis_for_a_recalculation():
    _, trace = LevelsRrProfile().plan_with_trace(_levels_context(atr="0.012"))

    assert trace.outcome is TraceOutcome.ACCEPTED
    assert trace.result.value["stop_basis"] == "atr-floor"
    assert trace.result.value["stop"] == "3.012"
