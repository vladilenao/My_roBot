from __future__ import annotations

import pandas as pd

from src.strategies.base_strategy import StrategyConfig
from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.ma import MaCloudIndicator
from src.strategies.indicators.ma.signalEnum import MaCloudSignalEnum
from src.strategies.indicators.macd import MacdIndicator, MacdMode
from src.strategies.indicators.macd.signalEnum import MacdZeroCrossSignalEnum
from src.strategies.indicators.rsi import RsiIndicator
from src.strategies.indicators.rsi.signalEnum import RsiSignalEnum
from src.strategies.registry import register
from src.logging_setup import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ СТРАТЕГИИ
# MA Cloud: SMA 10/40 (пересечение облака)
# RSI: period=14, сигнальный уровень 50
# MACD Zero Cross: 12/26/9 (пересечение сигнальной линией нуля)
# Window: 1 (решение на текущей свече)
# ══════════════════════════════════════════════════════════════

DEFAULT_CONFIG = StrategyConfig(
    name="ma_cloud_rsi_macd",
    strategy_window=1,
    indicators=(
        MaCloudIndicator(fast_period=10, slow_period=40),
        RsiIndicator(period=14),
        MacdIndicator(fast=12, slow=26, signal=9, mode=MacdMode.ZERO_CROSS),
    ),
)

_ENTRY_WINDOW = 6  # макс. свечей для фиксации предыдущих сигналов


@register
class MaCloudRsiMacdStrategy:
    """Двусторонняя трендовая стратегия MA Cloud RSI MACD.

    Вход: третий индикатор (MA облако, RSI 50, MACD 0) подтверждает направление.
    Добор: ретест облака (цена в пространстве SMA 10..SMA 40).
    Выход: ступенчатый по MA 10/40/облаку в зависимости от размера позиции.
    """

    NAME = "ma_cloud_rsi_macd"
    STRATEGY_WINDOW = 1

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self.NAME = self._config.name
        self.STRATEGY_WINDOW = self._config.strategy_window
        self._pending_long: list[int] = []  # bar indices сработавших индикаторов Long
        self._pending_short: list[int] = []
        self._long_contracts = 0
        self._short_contracts = 0

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        for indicator in self._config.indicators:
            data = indicator.compute(data)
        return data

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        if len(ta) < 3:
            return Decision(
                SignalType.HOLD,
                float(ta.iloc[-1]["close"]) if len(ta) > 0 else 0.0,
                timeframe=timeframe,
                strategy_name=self.NAME,
            )

        row = ta.iloc[-1]
        bar_idx = len(ta) - 1
        price = float(row["close"])

        ma_sig = int(row["ma_cloud_signal"])
        rsi_sig = int(row["rsi_signal"])
        macd_sig = int(row["macd_zero_signal"])

        sma_fast = float(row["sma_fast"]) if pd.notna(row["sma_fast"]) else None
        sma_slow = float(row["sma_slow"]) if pd.notna(row["sma_slow"]) else None

        indicators = {}
        if sma_fast is not None:
            indicators["sma_fast"] = sma_fast
        if sma_slow is not None:
            indicators["sma_slow"] = sma_slow
        indicators["rsi"] = float(row["rsi"]) if pd.notna(row.get("rsi")) else 0.0

        # ── Очистка устаревших pending-сигналов ──────────────────
        self._pending_long = [
            i for i in self._pending_long if bar_idx - i < _ENTRY_WINDOW
        ]
        self._pending_short = [
            i for i in self._pending_short if bar_idx - i < _ENTRY_WINDOW
        ]

        # ── Определяем какие индикаторы сработали на этой свече ──
        signals_map = {
            "ma_cloud": (
                ma_sig,
                MaCloudSignalEnum.MA_CROSS_UP,
                MaCloudSignalEnum.MA_CROSS_DOWN,
            ),
            "rsi": (
                rsi_sig,
                RsiSignalEnum.CROSS_ABOVE_50,
                RsiSignalEnum.CROSS_BELOW_50,
            ),
            "macd_zero": (
                macd_sig,
                MacdZeroCrossSignalEnum.MACD_CROSS_ABOVE_ZERO,
                MacdZeroCrossSignalEnum.MACD_CROSS_BELOW_ZERO,
            ),
        }

        for name, (sig, bull, bear) in signals_map.items():
            if sig == bull and name not in self._pending_long:
                self._pending_long.append(bar_idx)
            if sig == bear and name not in self._pending_short:
                self._pending_short.append(bar_idx)

        # ── Вход: третий индикатор подтверждает ──────────────────
        if len(self._pending_long) >= 3 and self._long_contracts == 0:
            self._pending_long.clear()
            self._long_contracts = 1
            log.info("Entry Long: third indicator confirmed")
            return Decision(
                SignalType.BUY,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
                indicator_values=indicators,
                action="entry",
            )

        if len(self._pending_short) >= 3 and self._short_contracts == 0:
            self._pending_short.clear()
            self._short_contracts = 1
            log.info("Entry Short: third indicator confirmed")
            return Decision(
                SignalType.SELL,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
                indicator_values=indicators,
                action="entry",
            )

        # ── Выход (exit) ────────────────────────────────────────
        if sma_fast is not None and sma_slow is not None:
            exit_decision = self._check_exit(
                price, sma_fast, sma_slow, timeframe, indicators
            )
            if exit_decision is not None:
                return exit_decision

        # ── Добор (scale-in) ────────────────────────────────────
        if sma_fast is not None and sma_slow is not None:
            scale_decision = self._check_scale_in(
                row, price, sma_fast, sma_slow, timeframe, indicators
            )
            if scale_decision is not None:
                return scale_decision

        return Decision(
            SignalType.HOLD,
            price,
            timeframe=timeframe,
            strategy_name=self.NAME,
        )

    def _check_exit(
        self,
        price: float,
        sma_fast: float,
        sma_slow: float,
        timeframe: str | None,
        indicators: dict,
    ) -> Decision | None:
        """Проверка условий выхода по закрытию свечи.

        Приоритеты (проверяются в порядке убывания):
          Для >1 контракта: ① below MA10 → partial  ② in cloud → partial  ③ below MA40 → full
          Для 1 контракта: только ③ below MA40 → full
        """
        cloud_low = min(sma_fast, sma_slow)
        cloud_high = max(sma_fast, sma_slow)
        in_cloud = cloud_low <= price <= cloud_high

        # ── Long выход ─────────────────────────────────────────
        if self._long_contracts > 0:
            if self._long_contracts > 1:
                if price < sma_fast:
                    log.info("Exit Long: close below MA10 (partial)")
                    self._long_contracts -= 1
                    return Decision(
                        SignalType.SELL,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_below_ma10",
                        exit_contracts=1,
                    )
                if in_cloud:
                    log.info("Exit Long: close inside cloud (partial)")
                    self._long_contracts -= 1
                    return Decision(
                        SignalType.SELL,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_inside_cloud",
                        exit_contracts=1,
                    )
            if price < sma_slow:
                log.info("Exit Long: close below MA40")
                self._long_contracts = 0
                self._pending_long.clear()
                return Decision(
                    SignalType.SELL,
                    price,
                    timeframe=timeframe,
                    strategy_name=self.NAME,
                    indicator_values=indicators,
                    exit_reason="close_below_ma40",
                )

        # ── Short выход (зеркально) ────────────────────────────
        if self._short_contracts > 0:
            if self._short_contracts > 1:
                if price > sma_fast:
                    log.info("Exit Short: close above MA10 (partial)")
                    self._short_contracts -= 1
                    return Decision(
                        SignalType.BUY,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_above_ma10",
                        exit_contracts=1,
                    )
                if in_cloud:
                    log.info("Exit Short: close inside cloud (partial)")
                    self._short_contracts -= 1
                    return Decision(
                        SignalType.BUY,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_inside_cloud",
                        exit_contracts=1,
                    )
            if price > sma_slow:
                log.info("Exit Short: close above MA40")
                self._short_contracts = 0
                self._pending_short.clear()
                return Decision(
                    SignalType.BUY,
                    price,
                    timeframe=timeframe,
                    strategy_name=self.NAME,
                    indicator_values=indicators,
                    exit_reason="close_above_ma40",
                )

        return None

    def _check_scale_in(
        self,
        row: pd.Series,
        price: float,
        sma_fast: float,
        sma_slow: float,
        timeframe: str | None,
        indicators: dict,
    ) -> Decision | None:
        """Проверка условий добора при ретесте облака."""
        cloud_low = min(sma_fast, sma_slow)
        cloud_high = max(sma_fast, sma_slow)

        # Long добор: Low касается облака, закрытие выше SMA slow
        if self._long_contracts > 0 and self._long_contracts <= 2:
            low = float(row["low"])
            if low <= cloud_high and price > sma_slow:
                max_add = max(1, self._long_contracts // 2)
                if max_add >= 1:
                    log.info("Scale-in Long: retest cloud")
                    self._long_contracts += 1
                    return Decision(
                        SignalType.BUY,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        action="scale_in",
                    )

        # Short добор: High касается облака, закрытие ниже SMA slow
        if self._short_contracts > 0 and self._short_contracts <= 2:
            high = float(row["high"])
            if high >= cloud_low and price < sma_slow:
                log.info("Scale-in Short: retest cloud")
                self._short_contracts += 1
                return Decision(
                    SignalType.SELL,
                    price,
                    timeframe=timeframe,
                    strategy_name=self.NAME,
                    indicator_values=indicators,
                    action="scale_in",
                )

        return None

    def required_history(self) -> int:
        return self._config.required_history
