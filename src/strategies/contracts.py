from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import pandas as pd

from src.strategies.names import StrategyName

DEFAULT_FILTER_PROFILE = "basic_levels"


@dataclass(frozen=True)
class Assignment:
    """Привязка стратегии к инструменту: имя стратегии + профиль фильтрации + таймфрейм.

    Идентичность привязки — кортеж (инструмент, стратегия, профиль, таймфрейм):
    дубли имени стратегии на одном инструменте допустимы при разных профилях
    или разных таймфреймах. Таймфрейм обязателен и разворачивается каскадом
    конфигурации (tf инлайна → timeframe тикера → глобальный TIMEFRAME).
    """

    strategy: StrategyName
    filter_profile: str = DEFAULT_FILTER_PROFILE
    timeframe: str = field(kw_only=True)


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    signal_type: SignalType
    price: float
    timeframe: str | None = None
    strategy_name: str | None = None
    indicator_values: dict[str, float] | None = None
    bar_time: pd.Timestamp | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_distance_pct: float | None = None
    tp_distance_pct: float | None = None
    sl_level_label: str | None = None
    tp_level_label: str | None = None
    trend_direction: str | None = None
    trend_confidence: float | None = None
    action: str | None = None
    exit_reason: str | None = None
    exit_contracts: int | None = None
    risk_pct: float | None = None
    risk_rub: float | None = None
    quantity: int | None = None


class Strategy(Protocol):
    NAME: str
    STRATEGY_WINDOW: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision: ...

    def required_history(self) -> int: ...
