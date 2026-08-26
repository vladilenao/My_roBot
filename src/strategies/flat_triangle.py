from __future__ import annotations

import pandas as pd

from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.bb import BollingerBandsIndicator
from src.strategies.indicators.rsi import RsiIndicator
from src.strategies.indicators.stochastic import StochasticIndicator
from src.strategies.indicators.stochastic.signalEnum import SignalMode
from src.strategies.registry import register
from src.strategies.strategy import StrategyConfig

# ══════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ СТРАТЕГИИ
# Bollinger Bands: length=20, std=2.0
# RSI: period=14
# Stochastic: k=5, d=3, smooth_k=3, signal_mode=KD_CROSSOVER
# Window: 1
# ══════════════════════════════════════════════════════════════

DEFAULT_CONFIG = StrategyConfig(
    name="flat_triangle",
    strategy_window=1,
    indicators=(
        BollingerBandsIndicator(length=20, std=2.0),
        RsiIndicator(period=14),
        StochasticIndicator(
            k=5, d=3, smooth_k=3, signal_mode=SignalMode.KD_CROSSOVER
        ),
    ),
)


@register
class FlatTriangleStrategy:
    """Стратегия для бокового движения рынка на основе BB, RSI и Stochastic.

    Использует компаунд-условие: все три индикатора должны сработать
    одновременно на текущей свече.
    """

    NAME = "flat_triangle"
    STRATEGY_WINDOW = 1

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self.NAME = self._config.name
        self.STRATEGY_WINDOW = self._config.strategy_window

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        for indicator in self._config.indicators:
            data = indicator.compute(data)
        return data

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        row = ta.iloc[-1]

        bb_lower = float(row["bbl_20_2.0"])
        bb_upper = float(row["bbu_20_2.0"])
        rsi = float(row["rsi"])

        stoch_k_col = "stochk_5_3_3"
        stoch_d_col = "stochd_5_3_3"
        stoch_k = float(row[stoch_k_col])
        stoch_d = float(row[stoch_d_col])
        stoch_k_prev = float(ta[stoch_k_col].iloc[-2])
        stoch_d_prev = float(ta[stoch_d_col].iloc[-2])

        price = float(row["close"])

        indicators = {
            "bb_lower": bb_lower,
            "bb_upper": bb_upper,
            "rsi": rsi,
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
        }

        buy = (
            price <= bb_lower
            and rsi <= 30
            and stoch_k < 20
            and stoch_k > stoch_d
            and stoch_k_prev <= stoch_d_prev
        )

        sell = (
            price >= bb_upper
            and rsi >= 70
            and stoch_k > 80
            and stoch_k < stoch_d
            and stoch_k_prev >= stoch_d_prev
        )

        if buy:
            return Decision(
                SignalType.BUY,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
                indicator_values=indicators,
            )
        if sell:
            return Decision(
                SignalType.SELL,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
                indicator_values=indicators,
            )
        return Decision(SignalType.HOLD, price, timeframe=timeframe, strategy_name=self.NAME)

    def required_history(self) -> int:
        return self._config.required_history
