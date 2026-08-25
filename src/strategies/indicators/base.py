from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Indicator(ABC):
    """Базовый класс всех индикаторов.

    Каждый подкласс — frozen dataclass с параметрами индикатора,
    методом compute() для вычисления на DataFrame и свойством
    warmup для определения глубины истории.
    """

    signal_column: str

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Количество баров для прогрева индикатора."""

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Вычисляет индикатор и добавляет signal_column на копии df."""
