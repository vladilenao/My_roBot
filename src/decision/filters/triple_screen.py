"""Профиль фильтрации `triple_screen`: методика Александра Элдера.

Три экрана:
  1. Направление тренда на старшем ТФ — наклон гистограммы MACD.
  2. Коррекция на среднем ТФ — положение Stochastic (%K) относительно зон.
  3. Вход на рабочем ТФ — входящий сигнал базовой стратегии.

Иерархия ТФ строится по множителю (дефолт 5): ``TF_int`` — ближайший
стандартный таймфрейм, строго больший ``work × multiplier``; ``TF_htf`` —
ближайший стандартный таймфрейм, строго больший ``int × multiplier``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

import pandas as pd

from src.logging_setup import get_logger
from src.scheduler.timing import tf_period_minutes
from src.strategies.contracts import Decision, SignalType
from src.strategies.indicators.macd.indicator import MacdIndicator
from src.strategies.indicators.stochastic.indicator import StochasticIndicator

log = get_logger(__name__)


class FrameProvider(Protocol):
    """Поставщик кадров закрытых свечей для данных-зависимых фильтров."""

    def frame_for(
        self,
        instrument,
        timeframe: str,
        *,
        max_close: datetime | None = None,
        min_bars: int = 0,
    ) -> pd.DataFrame: ...


@dataclass(frozen=True)
class TripleScreenParams:
    """Параметры профиля triple_screen из секции `[strategies.filter.triple_screen]`."""

    multiplier: int = 5
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_k: int = 14
    stoch_d: int = 3
    stoch_smooth_k: int = 3
    oversold: int = 20
    overbought: int = 80

    def __post_init__(self) -> None:
        if self.multiplier < 2:
            raise ValueError(f"triple_screen: множитель ТФ должен быть >= 2, получено {self.multiplier}")
        if self.macd_fast >= self.macd_slow:
            raise ValueError(
                f"triple_screen: macd_fast ({self.macd_fast}) должен быть < macd_slow ({self.macd_slow})"
            )
        if self.macd_signal >= self.macd_slow:
            raise ValueError(
                f"triple_screen: macd_signal ({self.macd_signal}) должен быть < macd_slow ({self.macd_slow})"
            )
        if self.stoch_k < self.stoch_d:
            raise ValueError(
                f"triple_screen: stoch_k ({self.stoch_k}) должен быть >= stoch_d ({self.stoch_d})"
            )
        if self.stoch_k < 0 or self.stoch_d < 0 or self.stoch_smooth_k < 0:
            raise ValueError("triple_screen: параметры Stochastic должны быть неотрицательными")
        if not (0 < self.oversold < 100 and 0 < self.overbought < 100):
            raise ValueError(
                f"triple_screen: пороги %K должны быть в (0, 100), "
                f"получены oversold={self.oversold}, overbought={self.overbought}"
            )
        if self.oversold >= self.overbought:
            raise ValueError(
                f"triple_screen: oversold ({self.oversold}) должен быть < overbought ({self.overbought})"
            )

    @classmethod
    def from_config(cls, section: Mapping[str, Any]) -> TripleScreenParams:
        """Собирает параметры из dict секции конфигурации; неизвестные ключи → ValueError."""
        allowed = {f.name for f in fields(cls)}
        unknown = set(section) - allowed
        if unknown:
            raise ValueError(
                f"Незнакомые ключи секции triple_screen: {sorted(unknown)}; "
                f"допустимые: {sorted(allowed)}"
            )
        return cls(**{k: v for k, v in section.items()})


def tf_hierarchy(work_tf: str, multiplier: int, ladder: Sequence[str]) -> tuple[str, str]:
    """Иерархия ТФ Элдера: ``(tf_int, tf_htf)`` — ближайшие стандартные ТФ строго больше шага.

    ``tf_int`` — ближайший ТФ лестницы (в порядке возрастания длительности),
    строго больший ``work × multiplier``; ``tf_htf`` — строго больший
    ``int × multiplier``. Если подходящего ТФ нет — ``ValueError``.
    """
    periods = {tf: tf_period_minutes(tf) for tf in ladder}
    if work_tf not in periods:
        raise ValueError(f"triple_screen: неизвестный рабочий таймфрейм {work_tf!r}")
    ordered = sorted(ladder, key=periods.__getitem__)

    def _step(tf: str, mult: int) -> str:
        threshold = periods[tf] * mult
        for candidate in ordered:
            if periods[candidate] > threshold:
                return candidate
        raise ValueError(
            f"нет стандартного таймфрейма строго больше {threshold} минут "
            f"(шаг {tf} × {mult}); рабочий ТФ {work_tf!r} несовместим с triple_screen"
        )

    tf_int = _step(work_tf, multiplier)
    tf_htf = _step(tf_int, multiplier)
    return tf_int, tf_htf


def _default_ladder() -> tuple[str, ...]:
    """Стандартная лестница ТФ (ключи `TIMEFRAMES`) при отсутствии явной в конструкторе."""
    from src.config import TIMEFRAMES

    return tuple(TIMEFRAMES)


@dataclass(frozen=True)
class TripleScreenFilter:
    """Профиль triple_screen: чистый трансформер Decision через три экрана.

    Зависимости (поставщик кадров старших ТФ, параметры, лестница ТФ) приходят
    через конструктор — фильтр не имеет доступа к порту/нотификатору/рыночному
    контексту как источнику данных. `ladder` — лестница стандартных ТФ для
    иерархии Элдера; при умолчании берутся ключи `TIMEFRAMES`.
    """

    provider: FrameProvider
    params: TripleScreenParams
    ladder: tuple[str, ...] = field(default_factory=_default_ladder)

    def apply(
        self,
        decision: Decision,
        ctx,
        instrument: str = "",
        timeframe: str = "",
    ) -> Decision:
        if decision.signal_type is SignalType.HOLD:
            return decision
        if not instrument or not timeframe:
            log.warning(
                "triple_screen: нет контекста привязки (instrument=%r, timeframe=%r) — "
                "сигнал %s отклонён.",
                instrument, timeframe, decision.signal_type.value,
            )
            return replace(decision, signal_type=SignalType.HOLD)

        try:
            tf_int, tf_htf = tf_hierarchy(timeframe, self.params.multiplier, self.ladder)
        except ValueError as exc:
            log.warning(
                "triple_screen: %s — сигнал %s отклонён.",
                exc, decision.signal_type.value,
            )
            return replace(decision, signal_type=SignalType.HOLD)

        max_close = decision.bar_time
        macd = MacdIndicator(
            fast=self.params.macd_fast,
            slow=self.params.macd_slow,
            signal=self.params.macd_signal,
        )
        htf = self.provider.frame_for(
            instrument, tf_htf, max_close=max_close, min_bars=macd.warmup + 2
        )
        screen1 = self._screen1_direction(htf, macd, tf_htf)

        stoch = StochasticIndicator(
            k=self.params.stoch_k,
            d=self.params.stoch_d,
            smooth_k=self.params.stoch_smooth_k,
        )
        intf = self.provider.frame_for(
            instrument, tf_int, max_close=max_close, min_bars=stoch.warmup + 1
        )
        screen2 = self._screen2_direction(intf, stoch, tf_int)

        if (
            decision.signal_type is SignalType.BUY
            and screen1 is SignalType.BUY
            and screen2 is SignalType.BUY
        ):
            return decision
        if (
            decision.signal_type is SignalType.SELL
            and screen1 is SignalType.SELL
            and screen2 is SignalType.SELL
        ):
            return decision
        return replace(decision, signal_type=SignalType.HOLD)

    # ── экран 1: направление на старшем ТФ ──
    def _screen1_direction(
        self, frame: pd.DataFrame, macd: MacdIndicator, tf_htf: str
    ) -> SignalType | None:
        if frame is None or frame.empty or len(frame) < macd.warmup + 2:
            log.warning(
                "triple_screen: недостаточно закрытых свечей %s для экрана 1 — "
                "направление не подтверждено.", tf_htf,
            )
            return None
        hist = macd.compute(frame)[
            f"macdh_{self.params.macd_fast}_{self.params.macd_slow}_{self.params.macd_signal}"
        ].dropna()
        if len(hist) < 2:
            log.warning("triple_screen: не хватает гистограммы MACD на %s.", tf_htf)
            return None
        if hist.iloc[-1] > hist.iloc[-2]:
            return SignalType.BUY
        if hist.iloc[-1] < hist.iloc[-2]:
            return SignalType.SELL
        return None

    # ── экран 2: коррекция на среднем ТФ ──
    def _screen2_direction(
        self, frame: pd.DataFrame, stoch: StochasticIndicator, tf_int: str
    ) -> SignalType | None:
        if frame is None or frame.empty or len(frame) < stoch.warmup + 1:
            log.warning(
                "triple_screen: недостаточно закрытых свечей %s для экрана 2 — "
                "коррекция не подтверждена.", tf_int,
            )
            return None
        k_col = f"stochk_{self.params.stoch_k}_{self.params.stoch_d}_{self.params.stoch_smooth_k}"
        k = stoch.compute(frame)[k_col].dropna()
        if len(k) < 1:
            log.warning("triple_screen: не хватает %K на %s.", tf_int)
            return None
        last = k.iloc[-1]
        if last < self.params.oversold:
            return SignalType.BUY
        if last > self.params.overbought:
            return SignalType.SELL
        return None