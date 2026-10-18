import pytest

from src.market_context.lazy import LazyMarketContext, MarketContextError
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult


def _context() -> MarketContext:
    return MarketContext(
        trend=TrendResult(TrendDirection.UP, 0.5),
        sr_levels=[SRLevel(price=100.0, sr_type=SRType.SUPPORT, strength=2, label="S1")],
        current_price=101.0,
    )


class TestLazyMarketContext:
    def test_not_loaded_until_field_read(self):
        calls = []
        lazy = LazyMarketContext(lambda: calls.append(1) or _context())

        assert calls == []
        assert lazy.loaded is False
        assert "не вычислен" in repr(lazy)

    def test_fields_delegate_to_loaded_context(self):
        expected = _context()
        lazy = LazyMarketContext(lambda: expected)

        assert lazy.trend is expected.trend
        assert lazy.sr_levels is expected.sr_levels
        assert lazy.current_price == expected.current_price
        assert lazy.resolve() is expected

    def test_loads_once_and_reports_once(self):
        calls, reported = [], []
        lazy = LazyMarketContext(lambda: calls.append(1) or _context(), on_load=reported.append)

        lazy.trend
        lazy.sr_levels
        lazy.current_price

        assert calls == [1]
        assert len(reported) == 1
        assert repr(lazy) == repr(reported[0])

    def test_load_failure_is_not_mistaken_for_strategy_error(self):
        def broken():
            raise KeyError("close")

        lazy = LazyMarketContext(broken)

        with pytest.raises(MarketContextError) as caught:
            lazy.trend
        assert not isinstance(caught.value, (ValueError, TypeError, KeyError))
        assert isinstance(caught.value.__cause__, KeyError)
