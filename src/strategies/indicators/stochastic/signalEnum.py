from enum import Enum, IntEnum

from src.strategies.indicators.base import BaseSignalEnum


class SignalMode(Enum):
    """Режим генерации сигналов Stochastic."""
    EXIT_FROM_ZONES = "exit_from_zones"
    KD_CROSSOVER = "kd_crossover"


class StochasticSignalEnum(BaseSignalEnum, IntEnum):
    """Типы сигналов Stochastic индикатора."""

    EXIT_OVERSOLD = 1  # Выход из перепроданности: предыдущая K < 20, текущая K > 20, K растёт
    EXIT_OVERBOUGHT = -1  # Выход из перекупленности: предыдущая K > 80, текущая K < 80, K падает
    CROSS_UP = 1  # K пересекает D снизу вверх
    CROSS_DOWN = -1  # K пересекает D сверху вниз
    NO_SIGNAL = 0
