from enum import IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class MaCloudSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов MA Cloud индикатора."""

    MA_CROSS_UP = 1  # SMA 10 пересекает SMA 40 снизу вверх
    MA_CROSS_DOWN = -1  # SMA 10 пересекает SMA 40 сверху вниз
    NO_SIGNAL = 0
