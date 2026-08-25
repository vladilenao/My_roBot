from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategies.contracts import Decision, SignalType
from src.strategies.registry import register
from src.strategies.indicators.base import Indicator
from src.strategies.signals import get_last_signals
from src.strategies.strategy import StrategyConfig

EVENT_COLUMNS_TEMPLATE = ["datetime", "signal", "price", "{sums}"]


def _build_default_config() -> StrategyConfig:
    """Создаёт конфигурацию, соответствующую текущему хардкоду."""
    from src.strategies.indicators.macd import MacdIndicator
    from src.strategies.indicators.rsi import RsiIndicator
    from src.strategies.indicators.stochastic import StochasticIndicator

    return StrategyConfig(
        name="macd_rsi_stoch",
        strategy_window=5,
        indicators=(
            MacdIndicator(),
            RsiIndicator(),
            StochasticIndicator(),
        ),
    )


@register
class MacdRsiStochStrategy:
    """Стратегия, работающая с StrategyConfig.

    Принимает конфигурацию через конструктор. Если конфиг не передан,
    используется конфигурация по умолчанию (MACD 12/26/9, RSI 14,
    Stoch 14/3/3, window=5).
    """

    NAME = "macd_rsi_stoch"
    STRATEGY_WINDOW = 5

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or _build_default_config()
        self.NAME = self._config.name
        self.STRATEGY_WINDOW = self._config.strategy_window

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        for indicator in self._config.indicators:
            data = indicator.compute(data)
        return data

    def decide(self, ta: pd.DataFrame) -> Decision:
        sums = get_last_signals(
            ta, self.STRATEGY_WINDOW, self._config.signal_columns
        )
        current_price = float(ta["close"].iloc[-1])

        if all(s > 0 for s in sums):
            return Decision(SignalType.BUY, current_price)
        if all(s < 0 for s in sums):
            return Decision(SignalType.SELL, current_price)
        return Decision(SignalType.HOLD, current_price)

    def expected_events(self, ta: pd.DataFrame) -> pd.DataFrame:
        signal_columns = self._config.signal_columns
        sum_columns = [f"{col}_sum" for col in signal_columns]
        output_columns = [col.replace("_signal", "") for col in sum_columns]

        sums = ta[signal_columns].rolling(self.STRATEGY_WINDOW).sum()
        sums.columns = sum_columns

        events = pd.DataFrame(
            {
                "datetime": ta["datetime"],
                "price": ta["close"],
                **{col: sums[col] for col in sum_columns},
            }
        ).dropna(subset=sum_columns)

        buy_mask = pd.Series(True, index=events.index)
        sell_mask = pd.Series(True, index=events.index)
        for col in sum_columns:
            buy_mask &= events[col] > 0
            sell_mask &= events[col] < 0

        events = events[buy_mask | sell_mask].copy()
        events["signal"] = pd.Series(
            np.where(buy_mask[buy_mask | sell_mask], "BUY", "SELL"),
            index=events.index,
            dtype="string",
        )

        rename_map = dict(zip(sum_columns, output_columns))
        events = events.rename(columns=rename_map)

        result_columns = ["datetime", "signal", "price"] + output_columns
        return events[result_columns].reset_index(drop=True)

    def required_history(self) -> int:
        return self._config.required_history
