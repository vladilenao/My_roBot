import numpy as np
import pandas as pd

from src.strategies import register
from src.strategies.base import Decision, SignalType
from src.strategies.macd_rsi_stoch.indicators.calculator import tech_analyze
from src.strategies.macd_rsi_stoch.signals.aggregate import get_last_signals

EVENT_COLUMNS = ["datetime", "signal", "price", "macd_sum", "rsi_sum", "stoch_sum"]
SIGNAL_COLUMNS = ["macd_signal", "rsi_signal", "stoch_signal"]


@register
class MacdRsiStochStrategy:
    NAME = "macd_rsi_stoch"
    STRATEGY_WINDOW = 5

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return tech_analyze(df)

    def decide(self, ta: pd.DataFrame) -> Decision:
        macd_sum, rsi_sum, stoch_sum = get_last_signals(ta, self.STRATEGY_WINDOW)
        current_price = float(ta["close"].iloc[-1])

        if macd_sum > 0 and rsi_sum > 0 and stoch_sum > 0:
            return Decision(SignalType.BUY, current_price)
        if macd_sum < 0 and rsi_sum < 0 and stoch_sum < 0:
            return Decision(SignalType.SELL, current_price)
        return Decision(SignalType.HOLD, current_price)

    def expected_events(self, ta: pd.DataFrame) -> pd.DataFrame:
        sums = ta[SIGNAL_COLUMNS].rolling(self.STRATEGY_WINDOW).sum()
        events = pd.DataFrame(
            {
                "datetime": ta["datetime"],
                "price": ta["close"],
                "macd_sum": sums["macd_signal"],
                "rsi_sum": sums["rsi_signal"],
                "stoch_sum": sums["stoch_signal"],
            }
        ).dropna(subset=["macd_sum", "rsi_sum", "stoch_sum"])

        buy = (events["macd_sum"] > 0) & (events["rsi_sum"] > 0) & (events["stoch_sum"] > 0)
        sell = (events["macd_sum"] < 0) & (events["rsi_sum"] < 0) & (events["stoch_sum"] < 0)
        events = events[buy | sell].copy()
        events["signal"] = pd.Series(
            np.where(events["macd_sum"] > 0, "BUY", "SELL"),
            index=events.index,
            dtype="string",
        )
        return events[EVENT_COLUMNS].reset_index(drop=True)

    def required_history(self) -> int:
        """Окно агрегации + прогрев MACD(12,26,9): slow 26 + signal 9."""

        return self.STRATEGY_WINDOW + 35
