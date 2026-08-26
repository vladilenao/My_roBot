from enum import IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class MacdSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов MACD индикатора."""

    BULLISH_CROSSOVER_BELOW_ZERO = 1  # Бычий кроссовер ниже нуля: macds растёт, > macd, обе < 0
    BEARISH_CROSSOVER_ABOVE_ZERO = -1  # Медвежий кроссовер выше нуля: macds падает, < macd, обе > 0
    NO_SIGNAL = 0
