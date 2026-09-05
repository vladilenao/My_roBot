
from src.decision import SignalFilter
from src.market_context.models import MarketContext, TrendDirection, TrendResult
from src.strategies.contracts import Decision, SignalType


def _ctx(direction, strength=0.8, sr_levels=None) -> MarketContext:
    return MarketContext(
        trend=TrendResult(direction=direction, strength=strength),
        sr_levels=sr_levels or [],
        current_price=100.0,
    )


def _decision(sig=SignalType.BUY) -> Decision:
    return Decision(signal_type=sig, price=100.0)


class TestSignalFilter:
    def test_down_trend_blocks_buy(self):
        result = SignalFilter().apply(_decision(SignalType.BUY), _ctx(TrendDirection.DOWN))
        assert result.signal_type is SignalType.HOLD
        assert result.trend_confidence == 0.0

    def test_up_trend_blocks_sell(self):
        result = SignalFilter().apply(_decision(SignalType.SELL), _ctx(TrendDirection.UP))
        assert result.signal_type is SignalType.HOLD
        assert result.trend_confidence == 0.0

    def test_up_trend_allows_buy(self):
        result = SignalFilter().apply(_decision(SignalType.BUY), _ctx(TrendDirection.UP))
        assert result.signal_type is SignalType.BUY

    def test_down_trend_allows_sell(self):
        result = SignalFilter().apply(_decision(SignalType.SELL), _ctx(TrendDirection.DOWN))
        assert result.signal_type is SignalType.SELL

    def test_flat_trend_passes_any_signal(self):
        for sig in (SignalType.BUY, SignalType.SELL):
            result = SignalFilter().apply(_decision(sig), _ctx(TrendDirection.FLAT))
            assert result.signal_type is sig
            assert result.trend_direction == "flat"

    def test_hold_not_filtered(self):
        result = SignalFilter().apply(_decision(SignalType.HOLD), _ctx(TrendDirection.UP))
        assert result.signal_type is SignalType.HOLD
        assert result.trend_confidence == 0.0

    def test_enriches_trend_fields_on_pass(self):
        result = SignalFilter().apply(_decision(SignalType.BUY), _ctx(TrendDirection.UP, strength=0.6))
        assert result.trend_direction == "up"
        assert result.trend_confidence == 0.6

    def test_no_execution_port_called(self):
        filter_ = SignalFilter()
        decision = _decision(SignalType.BUY)
        result = filter_.apply(decision, _ctx(TrendDirection.DOWN))
        assert result is not decision
