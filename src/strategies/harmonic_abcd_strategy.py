"""Стратегия harmonic_abcd — торговля по гармонической фибо-формации AB=CD.

Сигнал строится детектором `market_structure.harmonic` (стратегия 0.2):
BUY на баре подтверждения бычьей формации, SELL на баре медвежьей,
HOLD в остальных случаях. Одно событие на формацию; при конфликте
лонг/шорт на одном баре приоритет у более поздней точки C.
"""

from __future__ import annotations

import pandas as pd

from src.market_structure.harmonic import (
    Direction,
    HarmonicPatternDetector,
)
from src.strategies.contracts import Decision, SignalType
from src.strategies.registry import register
from src.strategies.base_strategy import StrategyConfig

SIGNAL_COLUMN = "harmonic_signal"

DEFAULT_CONFIG = StrategyConfig(
    name="harmonic_abcd",
    strategy_window=1,
    indicators=(),
)


@register
class HarmonicAbcdStrategy:
    """Стратегия на основе формации AB=CD (стратегия 0.2).

    Вход от точки C после подтверждения формации, выход в цели D.
    Сигнальная колонка задаётся самой стратегией, индикаторы не используются.
    """

    NAME = "harmonic_abcd"
    STRATEGY_WINDOW = 1

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self.NAME = self._config.name
        self.STRATEGY_WINDOW = self._config.strategy_window
        self._detector = HarmonicPatternDetector()

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data[SIGNAL_COLUMN] = 0
        col = data.columns.get_loc(SIGNAL_COLUMN)

        patterns = self._detector.analyze(df)
        if not patterns:
            return data

        events: dict[int, Direction] = {}
        for pattern in patterns:
            # бар подтверждения формации — первый бар, где свинг C полностью
            # сформирован (вален правый соседний окно детектора)
            bar = pattern.c.index + self._detector.right
            if bar >= len(df):
                continue
            close = float(df["close"].iloc[bar])
            if pattern.direction is Direction.LONG:
                if not (close >= pattern.c.price and close < pattern.d_target):
                    continue
            else:
                if not (close <= pattern.c.price and close > pattern.d_target):
                    continue
            events[bar] = pattern.direction
        # при конфликте лонг/шорт на одном баре приоритет у лонга (детерминизм)
        for bar, direction in sorted(events.items()):
            data.iat[bar, col] = 1 if direction is Direction.LONG else -1
        return data

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        row = ta.iloc[-1]
        price = float(row["close"])
        signal = int(row[SIGNAL_COLUMN])
        if signal == 1:
            return Decision(SignalType.BUY, price, timeframe=timeframe, strategy_name=self.NAME)
        if signal == -1:
            return Decision(SignalType.SELL, price, timeframe=timeframe, strategy_name=self.NAME)
        return Decision(SignalType.HOLD, price, timeframe=timeframe, strategy_name=self.NAME)

    def required_history(self) -> int:
        return self._detector.warmup