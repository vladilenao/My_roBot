from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
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
SIGNAL_COLUMNS = ["ma_cloud_signal", "rsi_signal", "macd_zero_signal"]
# Сколько последних проигрышей держать: одна стратегия ТФ обслуживает
# несколько инструментов, у каждого свой кадр.
REPLAY_SLOTS = 8
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


@dataclass(frozen=True)
class _Replay:
    """Проигранный префикс кадра: сигналы строк и состояние перед последней."""

    timeframe: str | None
    signals: np.ndarray
    state_before_last: MaCloudState


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
        self._replays: list[_Replay] = []

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
        """Решение по последнему бару; уже проигранный префикс кадра не пересчитывается.

        Состояние зависит только от сигнальных столбцов и номера строки, поэтому
        проигрыш продолжается с запомненного состояния, если сигналы префикса
        совпадают с прежними. Иначе (новый контракт, правка истории, сдвиг
        начала кадра) кадр проигрывается целиком.
        """
        decision = Decision(SignalType.HOLD, float(ta.iloc[-1]["close"]) if len(ta) else 0.0, timeframe=timeframe, strategy_name=self.NAME)
        if not len(ta):
            return decision
        signals = self._signals(ta)
        start, state = self._resume(signals, timeframe) if signals is not None else (0, _EMPTY_STATE)
        before_last = state
        for i in range(start, len(ta)):
            before_last = state
            state, decision = self._step(state, i, ta.iloc[i], timeframe)
        if signals is not None:
            self._remember(_Replay(timeframe, signals, before_last))
        if decision.signal_type is SignalType.BUY:
            log.info("Entry Long: third indicator confirmed")
        elif decision.signal_type is SignalType.SELL:
            log.info("Entry Short: third indicator confirmed")
        return decision

    @staticmethod
    def _signals(ta: pd.DataFrame) -> np.ndarray | None:
        """Сигнальные столбцы как float; ``None`` — сравнить нельзя, проигрыш целиком."""
        if not set(SIGNAL_COLUMNS) <= set(ta.columns):
            return None
        try:
            return ta[SIGNAL_COLUMNS].to_numpy(dtype=float)
        except (TypeError, ValueError):
            return None

    def _resume(self, signals: np.ndarray, timeframe: str | None) -> tuple[int, MaCloudState]:
        """Строка, с которой продолжить проигрыш, и состояние перед ней."""
        best: _Replay | None = None
        for replay in self._replays:
            size = len(replay.signals)
            if (replay.timeframe == timeframe and size <= len(signals)
                    and (best is None or size > len(best.signals))
                    and np.array_equal(signals[:size], replay.signals, equal_nan=True)):
                best = replay
        if best is None:
            return 0, _EMPTY_STATE
        return len(best.signals) - 1, best.state_before_last

    def _remember(self, replay: _Replay) -> None:
        kept = [
            other for other in self._replays
            if not (other.timeframe == replay.timeframe and len(other.signals) <= len(replay.signals)
                    and np.array_equal(replay.signals[:len(other.signals)], other.signals, equal_nan=True))
        ]
        self._replays = [*kept, replay][-REPLAY_SLOTS:]

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
            return replace(state, pending_long=()), Decision(SignalType.BUY, price, bar_time, f"{self.NAME}:{timeframe or ''}:{event_key}:BUY", bar_time, timeframe, self.NAME, indicators, {"entry_reference": "high"})
        if len(state.pending_short) >= 3:
            price = float(row["low"])
            return replace(state, pending_short=()), Decision(SignalType.SELL, price, bar_time, f"{self.NAME}:{timeframe or ''}:{event_key}:SELL", bar_time, timeframe, self.NAME, indicators, {"entry_reference": "low"})
        return state, hold

    def required_history(self) -> int:
        return self._config.required_history
