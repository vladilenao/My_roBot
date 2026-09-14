"""Провайдер выровненных кадров старших таймфреймов для данных-зависимых фильтров.

Кадры неактивных ТФ (например `4h`) берутся по требованию из общего кэша:
первый запрос загружает историю целиком, последующие — инкрементально.
Поставщик срезает будущее (заглядывание вперёд запрещено): включаются только
закрытые свечи со временем закрытия не позже ``max_close`` (момент закрытия
рабочей свечи текущего тика) и сообщает о нехватке баров под прогрев.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)


class HtfFrameProvider:
    """Адаптер `MarketDataCache` + планировщик под контракт `FrameProvider` фильтра."""

    def __init__(self, cache, timeline) -> None:
        self._cache = cache
        self._timeline = timeline

    def frame_for(
        self,
        instrument,
        timeframe: str,
        *,
        max_close: datetime | None = None,
        min_bars: int = 0,
    ) -> pd.DataFrame:
        """Кадр закрытых свечей ``timeframe``, выровненный до ``max_close``.

        Возвращает только свечи, закрывшиеся не позднее ``max_close``
        (время закрытия рабочей свечи тика). Если ``max_close`` не задан —
        свечи, закрытые к текущему моменту планировщика.
        """
        self._cache.ensure_loaded(instrument, timeframe)
        frame = self._cache.frame_for(instrument, timeframe)
        if frame is None or frame.empty:
            return pd.DataFrame()

        grid = self._timeline.grid(timeframe)
        if max_close is not None:
            bound = to_naive(max_close)
            keep = frame["datetime"].apply(
                lambda ts: pd.Timestamp(grid.bar_close(ts)) <= bound
            )
            frame = frame[keep].reset_index(drop=True)

        if min_bars and len(frame) < min_bars:
            log.warning(
                "Кадр %s для %s слишком короткий для прогрева: %s баров (нужно >= %s).",
                timeframe,
                getattr(instrument, "label", instrument),
                len(frame),
                min_bars,
            )
        return frame