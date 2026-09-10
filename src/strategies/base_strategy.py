from __future__ import annotations

from dataclasses import dataclass

from src.strategies.indicators.base import Indicator


@dataclass(frozen=True)
class StrategyConfig:
    """Иммутабельная конфигурация торговой стратегии.

    Содержит имя, окно агрегации и кортеж индикаторов.
    Все вычисляемые свойства (required_history, signal_columns)
    выводятся из параметров индикаторов.
    """

    name: str
    strategy_window: int
    indicators: tuple[Indicator, ...]

    @property
    def required_history(self) -> int:
        """Минимальная глубина истории: окно + прогрев самого «медленного» индикатора."""
        if not self.indicators:
            return self.strategy_window
        return self.strategy_window + max(i.warmup for i in self.indicators)

    @property
    def signal_columns(self) -> list[str]:
        """Список имён сигнальных столбцов из всех индикаторов."""
        return [i.signal_column for i in self.indicators]


class StrategyBuilder:
    """Builder для StrategyConfig с method chaining."""

    def __init__(self) -> None:
        self._name: str | None = None
        self._strategy_window: int = 5
        self._indicators: list[Indicator] = []

    def set_name(self, name: str) -> StrategyBuilder:
        self._name = name
        return self

    def set_strategy_window(self, window: int) -> StrategyBuilder:
        self._strategy_window = window
        return self

    def add_indicator(self, indicator: Indicator) -> StrategyBuilder:
        self._indicators.append(indicator)
        return self

    def build(self) -> StrategyConfig:
        if not self._name:
            raise ValueError("Имя стратегии обязательно")
        if self._strategy_window <= 0:
            raise ValueError(
                f"strategy_window ({self._strategy_window}) должен быть > 0"
            )
        if not self._indicators:
            raise ValueError("Требуется хотя бы один индикатор")
        return StrategyConfig(
            name=self._name,
            strategy_window=self._strategy_window,
            indicators=tuple(self._indicators),
        )
