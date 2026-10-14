from .bearish_engulfing import BearishEngulfing
from .bullish_engulfing import BullishEngulfing

PATTERNS = (BullishEngulfing(), BearishEngulfing())

__all__ = ["PATTERNS", "BearishEngulfing", "BullishEngulfing"]
