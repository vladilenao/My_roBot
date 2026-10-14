from src.market_structure.patterns.bearish_engulfing import BearishEngulfing
from src.market_structure.patterns.bullish_engulfing import BullishEngulfing


def _candle(opening, closing):
    return {"open": opening, "high": max(opening, closing) + 1, "low": min(opening, closing) - 1, "close": closing}


def test_bullish_engulfing_includes_equal_body_bounds():
    result = BullishEngulfing().analyze([_candle(10, 9), _candle(9, 10)])

    assert result is not None
    assert result.event_type == "bullish_engulfing"
    assert result.direction == "long"
    assert result.body_overlap_ratio == 1.0


def test_bearish_engulfing_rejects_doji_and_insufficient_history():
    pattern = BearishEngulfing()

    assert pattern.analyze([]) is None
    assert pattern.analyze([_candle(9, 10), _candle(10, 10)]) is None
    assert pattern.analyze([_candle(9, 10), _candle(10, 9)]) is not None
