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
from src.logging_setup import get_logger

log = get_logger(__name__)

SIGNAL_COLUMN = "harmonic_signal"
PATTERN_COLUMNS = (
    "harmonic_pattern_id",
    "harmonic_a",
    "harmonic_b",
    "harmonic_c",
    "harmonic_d",
    "harmonic_time_available",
)

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
        for column in PATTERN_COLUMNS:
            data[column] = None
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
            confirmation_time = self._bar_time(df, bar)
            pattern_points = (pattern.x.index, pattern.a.index, pattern.b.index, pattern.c.index)
            pattern_id = (
                f"{self._detector.pattern}:{pattern.direction.value}:"
                + ":".join(self._point_id(df, index) for index in pattern_points)
            )
            data.loc[data.index[bar], list(PATTERN_COLUMNS)] = (
                pattern_id,
                pattern.a.price,
                pattern.b.price,
                pattern.c.price,
                pattern.d_target,
                confirmation_time,
            )
        # при конфликте лонг/шорт на одном баре приоритет у лонга (детерминизм)
        for bar, direction in sorted(events.items()):
            data.iat[bar, col] = 1 if direction is Direction.LONG else -1
        return data

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        row = ta.iloc[-1]
        price = float(row["close"])
        bar_time = pd.Timestamp(row["datetime"]) if "datetime" in row else None
        signal = int(row[SIGNAL_COLUMN])
        if signal == 1:
            return self._entry_decision(SignalType.BUY, price, bar_time, timeframe, row)
        if signal == -1:
            return self._entry_decision(SignalType.SELL, price, bar_time, timeframe, row)
        return Decision(SignalType.HOLD, price, bar_time=bar_time, timeframe=timeframe, strategy_name=self.NAME)

    @staticmethod
    def _bar_time(df: pd.DataFrame, index: int) -> pd.Timestamp | None:
        if "datetime" not in df:
            return None
        return pd.Timestamp(df["datetime"].iloc[index])

    @classmethod
    def _point_id(cls, df: pd.DataFrame, index: int) -> str:
        time = cls._bar_time(df, index)
        return time.isoformat() if time is not None else str(index)

    def _entry_decision(
        self,
        signal_type: SignalType,
        price: float,
        bar_time: pd.Timestamp | None,
        timeframe: str | None,
        row: pd.Series,
    ) -> Decision:
        references = self._references(row)
        # A formation, rather than its confirmation bar, is the idempotency unit.
        event_id = f"{self.NAME}:{timeframe or ''}:{references['pattern_id']}:{signal_type.value}"
        return Decision(
            signal_type,
            price,
            bar_time=bar_time,
            event_id=event_id,
            available_at=bar_time,
            timeframe=timeframe,
            strategy_name=self.NAME,
            idea_references=references,
        )

    @staticmethod
    def _references(row: pd.Series) -> dict[str, float | str]:
        time_available = row["harmonic_time_available"]
        return {
            "pattern_id": str(row["harmonic_pattern_id"]),
            "a": float(row["harmonic_a"]),
            "b": float(row["harmonic_b"]),
            "c": float(row["harmonic_c"]),
            "d": float(row["harmonic_d"]),
            "time_available": pd.Timestamp(time_available).isoformat()
            if time_available is not None
            else "",
        }

    def required_history(self) -> int:
        return self._detector.warmup
