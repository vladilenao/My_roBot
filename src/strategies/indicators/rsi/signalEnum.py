from enum import IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class RsiSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов RSI индикатора."""

    CROSS_ABOVE_50 = 1  # RSI пересекает 50 снизу вверх: предыдущий < 50, текущий > 50
    CROSS_BELOW_50 = -1  # RSI пересекает 50 сверху вниз: предыдущий > 50, текущий < 50
    NO_SIGNAL = 0
