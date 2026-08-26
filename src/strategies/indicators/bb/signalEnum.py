from enum import IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class BbSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов Bollinger Bands индикатора."""

    TOUCH_LOWER = 1  # Цена касается или ниже нижней полосы
    TOUCH_UPPER = -1  # Цена касается или выше верхней полосы
    NO_SIGNAL = 0  # Цена между полосами
