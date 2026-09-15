from __future__ import annotations

from dataclasses import dataclass, replace

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

EVENT_COLUMNS = [
    "datetime",
    "signal",
    "price",
    "action",
    "exit_reason",
]


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

ENTRY_WINDOW = 6  # макс. свечей для фиксации предыдущих сигналов


@dataclass(frozen=True)
class MaCloudState:
    """Внутреннее состояние стратегии как неизменяемая структура.

    Решение детерминировано накопительной историей: состояние восстанавливается
    реплеем правил по барам `ta` от его начала. Повторные вызовы `decide()` на
    одном инстансе идемпотентны, а привязки одного таймфрейма с разными
    профилями фильтрации «не съедают» состояние друг друга.
    """

    pending_long: tuple[tuple[str, int], ...] = ()
    pending_short: tuple[tuple[str, int], ...] = ()
    long_contracts: int = 0
    short_contracts: int = 0


_EMPTY_STATE = MaCloudState()


def _purge_pending(pending: tuple[tuple[str, int], ...], bar_idx: int) -> tuple[tuple[str, int], ...]:
    """Удаляет подтверждения, вышедшие за окно ENTRY_WINDOW (абсолютный bar_idx)."""
    return tuple((name, bar) for name, bar in pending if bar_idx - bar < ENTRY_WINDOW)


@register
class MaCloudRsiMacdStrategy:
    """Двусторонняя трендовая стратегия MA Cloud RSI MACD.

    Вход: три РАЗНЫХ индикатора (MA облако, RSI 50, MACD 0) подтверждают
    направление в пределах окна ENTRY_WINDOW; подтверждение учитывается
    один раз на индикатор.
    Добор: ретест облака (цена в пространстве SMA 10..SMA 40).
    Выход: ступенчатый по MA 10/40/облаку в зависимости от размера позиции.

    Семантика: `decide(ta)` — чистая функция накопительной истории. Внутреннее
    состояние (`pending`-сигналы, счётчики контрактов) не хранится в инстансе
    между вызовами, а восстанавливается реплеем правил по всем барам `ta`.
    Поэтому один инстанс может безопасно разделяться несколькими привязками
    одного таймфрейма, а повторные вызовы дают одинаковый результат.
    """

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
        """События BUY/SELL за всю историю (эталон snapshot-тестов).

        Однопроходный реплей правил `_step` по всем барам `ta` — та же
        семантика вызова `decide`, что и в боевом цикле, без накопления
        состояния в инстансе. События собираются начиная с конца прогрева
        индикаторов (`required_history() - 1`).
        """
        rows = []
        state = _EMPTY_STATE
        start = max(0, self.required_history() - 1)
        for i in range(len(ta)):
            state, decision = self._step(state, i, ta.iloc[i])
            if i < start or decision.signal_type is SignalType.HOLD:
                continue
            rows.append(
                {
                    "datetime": ta.iloc[i]["datetime"],
                    "signal": decision.signal_type.value,
                    "price": float(decision.price),
                    "action": decision.action or "",
                    "exit_reason": decision.exit_reason or "",
                }
            )
        # Всегда гарантируем схему событий: даже при нуле сигналов
        # возвращаем DataFrame с каноническими колонками, чтобы
        # snapshot-запись и сравнение пустых эталонов работали одинаково.
        events = pd.DataFrame(rows, columns=EVENT_COLUMNS)
        if len(events):
            events = events.astype(
                {
                    "signal": "string",
                    "action": "string",
                    "exit_reason": "string",
                }
            )
        else:
            events = events.astype(
                {
                    "datetime": "datetime64[ns]",
                    "signal": "string",
                    "price": "float64",
                    "action": "string",
                    "exit_reason": "string",
                }
            )
        return events

    def decide(self, ta: pd.DataFrame, timeframe: str | None = None) -> Decision:
        """Решение на последней свече, восстановленное реплеем всей истории.

        Однопроходный реплей `_step` с пустого состояния; возвращается решение
        последнего бара. Внутреннее состояние между вызовами не хранится.
        """
        state = _EMPTY_STATE
        decision = Decision(
            SignalType.HOLD,
            float(ta.iloc[-1]["close"]) if len(ta) > 0 else 0.0,
            timeframe=timeframe,
            strategy_name=self.NAME,
        )
        for i in range(len(ta)):
            state, decision = self._step(state, i, ta.iloc[i], timeframe=timeframe)
        return decision

    def _replay_state(self, ta: pd.DataFrame) -> MaCloudState:
        """Состояние стратегии после реплея всей истории (чистый результат)."""
        state = _EMPTY_STATE
        for i in range(len(ta)):
            state, _ = self._step(state, i, ta.iloc[i])
        return state

    def _decide_from_state(
        self,
        ta: pd.DataFrame,
        state: MaCloudState,
        timeframe: str | None = None,
    ) -> Decision:
        """Решение последнего бара из явно заданного состояния (тестовый шов).

        Полезен для проверки «при известном состоянии/позиции → известно
        решение»: применяет правила последнего бара без реплея истории.
        """
        return self._step(state, len(ta) - 1, ta.iloc[-1], timeframe=timeframe)[1]

    def _step(
        self,
        state: MaCloudState,
        bar_idx: int,
        row: pd.Series,
        timeframe: str | None = None,
    ) -> tuple[MaCloudState, Decision]:
        """Чистый шаг стратегии: (новое состояние, решение) для одной свечи.

        Воспроизводит правила боевого цикла в том же порядке: прогрев →
        пурж окна → регистрация подтверждений → вход → выход → добор.
        """
        price = float(row["close"])

        if bar_idx < 2:
            # Прогрев: недостаточно истории для решения — HOLD без мутаций.
            return state, Decision(
                SignalType.HOLD,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
            )

        # Сырые сигналы индикаторов до окончания прогрева — NaN: не обрабатываем.
        if pd.isna(row["ma_cloud_signal"]):
            return state, Decision(
                SignalType.HOLD,
                price,
                timeframe=timeframe,
                strategy_name=self.NAME,
            )

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
        # bar_idx абсолютен: реплей идёт по накопительной истории с её начала,
        # поэтому позиция свечи совпадает с её местом в полном фрейме.
        pending_long = _purge_pending(state.pending_long, bar_idx)
        pending_short = _purge_pending(state.pending_short, bar_idx)

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
            # Подтверждение учитывается по имени индикатора: повторный сигнал
            # того же индикатора не засчитывается вторым/третьим голосом.
            if sig == bull and name not in dict(pending_long):
                pending_long += ((name, bar_idx),)
            if sig == bear and name not in dict(pending_short):
                pending_short += ((name, bar_idx),)

        # Зафиксируем обновлённые pending-списки в состоянии (frozen dataclass).
        new_state = replace(state, pending_long=pending_long, pending_short=pending_short)

        # ── Вход: три РАЗНЫХ индикатора подтверждают направление ──
        if len(new_state.pending_long) >= 3 and new_state.long_contracts == 0:
            log.info("Entry Long: third indicator confirmed")
            return (
                replace(new_state, pending_long=(), long_contracts=1),
                Decision(
                    SignalType.BUY,
                    price,
                    timeframe=timeframe,
                    strategy_name=self.NAME,
                    indicator_values=indicators,
                    action="entry",
                ),
            )

        if len(new_state.pending_short) >= 3 and new_state.short_contracts == 0:
            log.info("Entry Short: third indicator confirmed")
            return (
                replace(new_state, pending_short=(), short_contracts=1),
                Decision(
                    SignalType.SELL,
                    price,
                    timeframe=timeframe,
                    strategy_name=self.NAME,
                    indicator_values=indicators,
                    action="entry",
                ),
            )

        # ── Выход (exit) ────────────────────────────────────────
        if sma_fast is not None and sma_slow is not None:
            new_state, exit_decision = self._check_exit(
                new_state, price, sma_fast, sma_slow, timeframe, indicators
            )
            if exit_decision is not None:
                return new_state, exit_decision

        # ── Добор (scale-in) ────────────────────────────────────
        if sma_fast is not None and sma_slow is not None:
            new_state, scale_decision = self._check_scale_in(
                new_state, row, price, sma_fast, sma_slow, timeframe, indicators
            )
            if scale_decision is not None:
                return new_state, scale_decision

        return new_state, Decision(
            SignalType.HOLD,
            price,
            timeframe=timeframe,
            strategy_name=self.NAME,
        )

    def _check_exit(
        self,
        state: MaCloudState,
        price: float,
        sma_fast: float,
        sma_slow: float,
        timeframe: str | None,
        indicators: dict,
    ) -> tuple[MaCloudState, Decision | None]:
        """Проверка условий выхода по закрытию свечи (чистый вариант).

        Приоритеты (проверяются в порядке убывания):
          Для >1 контракта: ① below MA10 → partial  ② in cloud → partial  ③ below MA40 → full
          Для 1 контракта: только ③ below MA40 → full
        """
        cloud_low = min(sma_fast, sma_slow)
        cloud_high = max(sma_fast, sma_slow)
        in_cloud = cloud_low <= price <= cloud_high

        # ── Long выход ─────────────────────────────────────────
        if state.long_contracts > 0:
            if state.long_contracts > 1:
                if price < sma_fast:
                    log.info("Exit Long: close below MA10 (partial)")
                    return (
                        replace(state, long_contracts=state.long_contracts - 1),
                        Decision(
                            SignalType.SELL,
                            price,
                            timeframe=timeframe,
                            strategy_name=self.NAME,
                            indicator_values=indicators,
                            exit_reason="close_below_ma10",
                            exit_contracts=1,
                        ),
                    )
                if in_cloud:
                    log.info("Exit Long: close inside cloud (partial)")
                    return (
                        replace(state, long_contracts=state.long_contracts - 1),
                        Decision(
                            SignalType.SELL,
                            price,
                            timeframe=timeframe,
                            strategy_name=self.NAME,
                            indicator_values=indicators,
                            exit_reason="close_inside_cloud",
                            exit_contracts=1,
                        ),
                    )
            if price < sma_slow:
                log.info("Exit Long: close below MA40")
                return (
                    replace(state, long_contracts=0, pending_long=()),
                    Decision(
                        SignalType.SELL,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_below_ma40",
                    ),
                )

        # ── Short выход (зеркально) ────────────────────────────
        if state.short_contracts > 0:
            if state.short_contracts > 1:
                if price > sma_fast:
                    log.info("Exit Short: close above MA10 (partial)")
                    return (
                        replace(state, short_contracts=state.short_contracts - 1),
                        Decision(
                            SignalType.BUY,
                            price,
                            timeframe=timeframe,
                            strategy_name=self.NAME,
                            indicator_values=indicators,
                            exit_reason="close_above_ma10",
                            exit_contracts=1,
                        ),
                    )
                if in_cloud:
                    log.info("Exit Short: close inside cloud (partial)")
                    return (
                        replace(state, short_contracts=state.short_contracts - 1),
                        Decision(
                            SignalType.BUY,
                            price,
                            timeframe=timeframe,
                            strategy_name=self.NAME,
                            indicator_values=indicators,
                            exit_reason="close_inside_cloud",
                            exit_contracts=1,
                        ),
                    )
            if price > sma_slow:
                log.info("Exit Short: close above MA40")
                return (
                    replace(state, short_contracts=0, pending_short=()),
                    Decision(
                        SignalType.BUY,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        exit_reason="close_above_ma40",
                    ),
                )

        return state, None

    def _check_scale_in(
        self,
        state: MaCloudState,
        row: pd.Series,
        price: float,
        sma_fast: float,
        sma_slow: float,
        timeframe: str | None,
        indicators: dict,
    ) -> tuple[MaCloudState, Decision | None]:
        """Проверка условий добора при ретесте облака (чистый вариант)."""
        cloud_low = min(sma_fast, sma_slow)
        cloud_high = max(sma_fast, sma_slow)

        # Long добор: Low касается облака, закрытие выше SMA slow
        if state.long_contracts > 0 and state.long_contracts <= 2:
            low = float(row["low"])
            if low <= cloud_high and price > sma_slow:
                max_add = max(1, state.long_contracts // 2)
                if max_add >= 1:
                    log.info("Scale-in Long: retest cloud")
                    return (
                        replace(state, long_contracts=state.long_contracts + 1),
                        Decision(
                            SignalType.BUY,
                            price,
                            timeframe=timeframe,
                            strategy_name=self.NAME,
                            indicator_values=indicators,
                            action="scale_in",
                        ),
                    )

        # Short добор: High касается облака, закрытие ниже SMA slow
        if state.short_contracts > 0 and state.short_contracts <= 2:
            high = float(row["high"])
            if high >= cloud_low and price < sma_slow:
                log.info("Scale-in Short: retest cloud")
                return (
                    replace(state, short_contracts=state.short_contracts + 1),
                    Decision(
                        SignalType.SELL,
                        price,
                        timeframe=timeframe,
                        strategy_name=self.NAME,
                        indicator_values=indicators,
                        action="scale_in",
                    ),
                )

        return state, None

    def required_history(self) -> int:
        return self._config.required_history