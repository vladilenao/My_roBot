from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from src.logging_setup import get_logger
from src.strategies.base_strategy import StrategyConfig
from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.ma import MaCloudIndicator
from src.strategies.indicators.ma.signalEnum import MaCloudSignalEnum
from src.strategies.indicators.macd import MacdIndicator, MacdMode
from src.strategies.indicators.macd.signalEnum import MacdZeroCrossSignalEnum
from src.strategies.indicators.rsi import RsiIndicator
from src.strategies.indicators.rsi.signalEnum import RsiSignalEnum
from src.strategies.registry import register

EVENT_COLUMNS = ["datetime", "signal", "price"]
ENTRY_WINDOW = 6
log = get_logger(__name__)

DEFAULT_CONFIG = StrategyConfig(
    name="ma_cloud_rsi_macd",
    strategy_window=1,
    indicators=(
        MaCloudIndicator(fast_period=10, slow_period=40),
        RsiIndicator(period=14),
        MacdIndicator(fast=12, slow=26, signal=9, mode=MacdMode.ZERO_CROSS),
    ),
)


@dataclass(frozen=True)
class MaCloudState:
    """Pending indicator confirmations reconstructed from the supplied history."""

    pending_long: tuple[tuple[str, int], ...] = ()
    pending_short: tuple[tuple[str, int], ...] = ()


_EMPTY_STATE = MaCloudState()


def _purge_pending(pending: tuple[tuple[str, int], ...], bar_idx: int) -> tuple[tuple[str, int], ...]:
    return tuple((name, bar) for name, bar in pending if bar_idx - bar < ENTRY_WINDOW)


@register
class MaCloudRsiMacdStrategy:
    """Entry-only MA Cloud/RSI/MACD strategy with three confirmations in six bars."""

    NAME = "ma_cloud_rsi_macd"
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

    def expected_events(self, ta: pd.DataFrame) -> pd.DataFrame:
        rows = []
        state = _EMPTY_STATE
        for i in range(len(ta)):
            state, decision = self._step(state, i, ta.iloc[i])
            if i >= self.required_history() - 1 and decision.signal_type is not SignalType.HOLD:
                rows.append({"datetime": ta.iloc[i]["datetime"], "signal": decision.signal_type.value, "price": decision.price})
        events = pd.DataFrame(rows, columns=EVENT_COLUMNS)
        if len(events):
            return events.astype({"signal": "string"})
        return events.astype({"datetime": "datetime64[ns]", "signal": "string", "price": "float64"})

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        state = _EMPTY_STATE
        decision = Decision(SignalType.HOLD, float(ta.iloc[-1]["close"]) if len(ta) else 0.0, timeframe=timeframe, strategy_name=self.NAME)
        for i in range(len(ta)):
            state, decision = self._step(state, i, ta.iloc[i], timeframe)
        return decision

    def _replay_state(self, ta: pd.DataFrame) -> MaCloudState:
        state = _EMPTY_STATE
        for i in range(len(ta)):
            state, _ = self._step(state, i, ta.iloc[i])
        return state

    def _step(self, state: MaCloudState, bar_idx: int, row: pd.Series, timeframe: str | None = None) -> tuple[MaCloudState, Decision]:
        close = float(row["close"])
        bar_time = pd.Timestamp(row["datetime"]) if "datetime" in row else None
        hold = Decision(SignalType.HOLD, close, bar_time, timeframe=timeframe, strategy_name=self.NAME)
        if bar_idx < 2 or pd.isna(row["ma_cloud_signal"]):
            return state, hold

        pending_long = _purge_pending(state.pending_long, bar_idx)
        pending_short = _purge_pending(state.pending_short, bar_idx)
        signals = {
            "ma_cloud": (int(row["ma_cloud_signal"]), MaCloudSignalEnum.MA_CROSS_UP, MaCloudSignalEnum.MA_CROSS_DOWN),
            "rsi": (int(row["rsi_signal"]), RsiSignalEnum.CROSS_ABOVE_50, RsiSignalEnum.CROSS_BELOW_50),
            "macd_zero": (int(row["macd_zero_signal"]), MacdZeroCrossSignalEnum.MACD_CROSS_ABOVE_ZERO, MacdZeroCrossSignalEnum.MACD_CROSS_BELOW_ZERO),
        }
        for name, (signal, bull, bear) in signals.items():
            if signal == bull and name not in dict(pending_long):
                pending_long += ((name, bar_idx),)
            if signal == bear and name not in dict(pending_short):
                pending_short += ((name, bar_idx),)
        state = replace(state, pending_long=pending_long, pending_short=pending_short)
        indicators = {"sma_fast": float(row["sma_fast"]), "sma_slow": float(row["sma_slow"]), "rsi": float(row["rsi"])}
        event_key = bar_time.isoformat() if bar_time is not None else str(bar_idx)
        if len(state.pending_long) >= 3:
            price = float(row["high"])
            log.info("Entry Long: third indicator confirmed")
            return replace(state, pending_long=()), Decision(SignalType.BUY, price, bar_time, f"{self.NAME}:{timeframe or ''}:{event_key}:BUY", bar_time, timeframe, self.NAME, indicators, {"entry_reference": "high"})
        if len(state.pending_short) >= 3:
            price = float(row["low"])
            log.info("Entry Short: third indicator confirmed")
            return replace(state, pending_short=()), Decision(SignalType.SELL, price, bar_time, f"{self.NAME}:{timeframe or ''}:{event_key}:SELL", bar_time, timeframe, self.NAME, indicators, {"entry_reference": "low"})
        return state, hold

    def required_history(self) -> int:
        return self._config.required_history
