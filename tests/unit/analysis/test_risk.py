import pytest

from src.analysis.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.analysis.risk import RiskManager
from src.strategies.contracts import Decision, SignalType


def _level(price, sr_type, label):
    return SRLevel(price=price, sr_type=sr_type, strength=2, label=label)


def _ctx(levels, price=100.0) -> MarketContext:
    return MarketContext(
        trend=TrendResult(direction=TrendDirection.UP, strength=0.8),
        sr_levels=levels,
        current_price=price,
    )


def _decision(sig=SignalType.BUY, price=100.0) -> Decision:
    return Decision(signal_type=sig, price=price)


class TestRiskManager:
    def test_buy_with_support_and_resistance(self):
        rm = RiskManager()
        ctx = _ctx([_level(97.0, SRType.SUPPORT, "S1"), _level(105.0, SRType.RESISTANCE, "R1")])
        result = rm.apply(_decision(SignalType.BUY), ctx)
        assert result.stop_loss == 97.0
        assert result.sl_level_label == "S1"
        assert result.take_profit == pytest.approx(106.0)
        assert result.sl_distance_pct == pytest.approx(0.03)
        assert result.tp_distance_pct == pytest.approx(0.06)

    def test_sell_with_resistance(self):
        rm = RiskManager()
        ctx = _ctx([_level(105.0, SRType.RESISTANCE, "R1")])
        result = rm.apply(_decision(SignalType.SELL), ctx)
        assert result.stop_loss == 105.0
        assert result.sl_level_label == "R1"
        assert result.take_profit == pytest.approx(90.0)
        assert result.sl_distance_pct == pytest.approx(0.05)

    def test_buy_no_support_uses_fallback(self):
        rm = RiskManager()
        ctx = _ctx([])
        result = rm.apply(_decision(SignalType.BUY, price=100.0), ctx)
        assert result.stop_loss == pytest.approx(98.0)
        assert result.sl_level_label is None

    def test_sell_no_resistance_uses_fallback(self):
        rm = RiskManager()
        ctx = _ctx([])
        result = rm.apply(_decision(SignalType.SELL, price=100.0), ctx)
        assert result.stop_loss == pytest.approx(102.0)
        assert result.sl_level_label is None

    def test_no_level_in_needed_direction_uses_fallback(self):
        rm = RiskManager()
        ctx = _ctx([_level(98.0, SRType.SUPPORT, "S1")])
        result = rm.apply(_decision(SignalType.BUY, price=100.0), ctx)
        assert result.stop_loss == pytest.approx(98.0)

        ctx2 = _ctx([_level(104.0, SRType.SUPPORT, "S1")])
        result2 = rm.apply(_decision(SignalType.SELL, price=100.0), ctx2)
        assert result2.stop_loss == pytest.approx(102.0)

    def test_tp_snaps_to_level_within_tolerance(self):
        rm = RiskManager()
        support = _level(99.0, SRType.SUPPORT, "S1")
        near_tp = _level(101.5, SRType.RESISTANCE, "R1")
        ctx = _ctx([support, near_tp], price=100.0)
        result = rm.apply(_decision(SignalType.BUY, price=100.0), ctx)
        assert result.take_profit == 101.5
        assert result.tp_level_label == "R1"

    def test_hold_unchanged(self):
        rm = RiskManager()
        decision = _decision(SignalType.HOLD)
        result = rm.apply(decision, _ctx([]))
        assert result is decision
        assert result.stop_loss is None
