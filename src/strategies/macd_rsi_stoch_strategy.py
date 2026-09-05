from __future__ import annotations

import pandas as pd

from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.macd import MacdIndicator
from src.strategies.indicators.rsi import RsiIndicator
from src.strategies.indicators.stochastic import StochasticIndicator
from src.strategies.registry import register
from src.strategies.signals import get_last_signals
from src.strategies.base_strategy import StrategyConfig

# ══════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ СТРАТЕГИИ
# MACD: fast=12, slow=26, signal=9
# RSI: period=14
# Stochastic: k=14, d=3, smooth_k=3
# Window: 5
# ══════════════════════════════════════════════════════════════

DEFAULT_CONFIG = StrategyConfig(
    name="macd_rsi_stoch",
    strategy_window=5,
    indicators=(
        MacdIndicator(fast=12, slow=26, signal=9),
        RsiIndicator(period=14),
        StochasticIndicator(k=14, d=3, smooth_k=3),
    ),
)


@register
class MacdRsiStochStrategy:
    """Стратегия, работающая с StrategyConfig.

    Принимает конфигурацию через конструктор. Конфиг обязателен.
    """

    NAME = "macd_rsi_stoch"
    STRATEGY_WINDOW = 5

    def __init__(self, config: StrategyConfig) -> None:
        self._config = config
        self.NAME = self._config.name
        self.STRATEGY_WINDOW = self._config.strategy_window

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        for indicator in self._config.indicators:
            data = indicator.compute(data)
        return data

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        sums = get_last_signals(
            ta, self.STRATEGY_WINDOW, self._config.signal_columns
        )
        current_price = float(ta["close"].iloc[-1])

        if all(s > 0 for s in sums):
            return Decision(SignalType.BUY, current_price, timeframe=timeframe, strategy_name=self.NAME)
        if all(s < 0 for s in sums):
            return Decision(SignalType.SELL, current_price, timeframe=timeframe, strategy_name=self.NAME)
        return Decision(SignalType.HOLD, current_price, timeframe=timeframe, strategy_name=self.NAME)

    def required_history(self) -> int:
        return self._config.required_history
