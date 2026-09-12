from enum import IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class MacdSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов MACD (режим кроссовера линий с фильтром зоны)."""

    BULLISH_CROSSOVER_BELOW_ZERO = (
        1  # Бычий кроссовер ниже нуля: macds растёт, > macd, обе < 0
    )
    BEARISH_CROSSOVER_ABOVE_ZERO = (
        -1
    )  # Медвежий кроссовер выше нуля: macds падает, < macd, обе > 0
    NO_SIGNAL = 0


class MacdZeroCrossSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов MACD (режим пересечения сигнальной линией нуля)."""

    MACD_CROSS_ABOVE_ZERO = 1  # Сигнальная линия MACD пересекает 0 снизу вверх
    MACD_CROSS_BELOW_ZERO = -1  # Сигнальная линия MACD пересекает 0 сверху вниз
    NO_SIGNAL = 0
