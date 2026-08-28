from __future__ import annotations

import pandas as pd

from src.data.timeutil import to_naive


def _naive(dt) -> pd.Timestamp:
    """Делегирует единому helper: приводит время к tz-naive pandas.Timestamp (UTC без пояса).

    Сохраняется как защита на границах готовности свечи: вход уже naive (no-op),
    но при возможном рецидиве aware-времени гарантирует целостность сравнений.
    """
    return to_naive(dt)


class MarketDataCache:
    """Кэш истории свечей: дозагрузка только новых закрытых баров, отдача закрытых свечей.

    Лениво загружает первый срез по инструменту, отдаёт стратегиям только готовые
    (закрытые) свечи и при появлении нового закрытого бара инкрементально дозагружает
    новые бары поверх кэша.
    """

    def __init__(self, loader, timeline, token=None) -> None:
        self._loader = loader
        self._timeline = timeline
        self._token = token
        self._frames: dict[tuple, pd.DataFrame | None] = {}
        self._instruments: dict[tuple, object] = {}
        self._last_loaded: dict[tuple, pd.Timestamp] = {}
        self._observed: dict[tuple, pd.Timestamp] = {}
        self._uids: dict[tuple, str] = {}

    def _key(self, instrument) -> tuple:
        return (instrument.ticker, instrument.instrument_type)

    def _load(self, instrument, start_date=None) -> pd.DataFrame:
        key = self._key(instrument)
        df, instrument_id = self._loader(
            ticker=instrument.ticker,
            instrument_type=instrument.instrument_type,
            timeframe=self._timeline.timeframe,
            start_date=start_date,
            end_date=None,
            token=self._token,
            instrument_id=self._uids.get(key),
        )
        if instrument_id is not None:
            self._uids[key] = instrument_id
        if df is not None and not df.empty and "datetime" in df.columns:
            df = df.sort_values("datetime").reset_index(drop=True)
        return df

    def frame_for(self, instrument) -> pd.DataFrame:
        """Возвращает готовые (закрытые) свечи инструмента, лениво загружая при первом запросе."""
        key = self._key(instrument)
        if key not in self._frames:
            self._initial_load(instrument, key)
        frame = self._frames[key]
        if frame is None or frame.empty:
            return pd.DataFrame()
        return self._closed_only(frame)

    def refresh_if_new_candle(self, now=None, force: bool = False) -> None:
        """Инкрементально дозагружает новые закрытые бары, если граница свечи сместилась.

        ``force=True`` заставляет повторно дозагружать поверх кэша даже если граница
        уже отслежена (используется при ожидании появления свежего закрытого бара,
        который из-за задержки публикации может быть временно недоступен).
        """
        boundary = _naive(self._timeline.current_candle_start(now or self._timeline.now()))
        for key in list(self._frames.keys()):
            if not force and self._observed.get(key, boundary) >= boundary:
                continue
            frame = self._frames[key]
            if frame is None or frame.empty:
                self._observed[key] = boundary
                continue
            last_dt = self._last_loaded.get(key)
            new_df = self._load(self._instruments[key], start_date=last_dt)
            merged = self._merge_new_bars(frame, new_df, last_dt)
            self._frames[key] = merged
            closed = self._closed_only(merged)
            self._last_loaded[key] = _naive(closed["datetime"].max()) if not closed.empty else last_dt
            self._observed[key] = boundary

    def has_fresh_closed_bar(self, now=None) -> bool:
        """Появился ли свежий закрытый бар текущей закрытой минуты во всех загруженных кэшах.

        Ожидаемый самый свежий закрытый бар начинается в
        ``previous_candle_start`` (``current_candle_start - period``). Возвращает
        ``True``, если в каждом загруженном фрейме есть закрытый бар не старше
        этой метки. Если загруженных инструментов нет — ``True`` (нечего ждать).
        """
        expected = _naive(
            self._timeline.previous_candle_start(now or self._timeline.now())
        )
        if not self._frames:
            return True
        for frame in self._frames.values():
            if frame is None or frame.empty:
                continue
            closed = self._closed_only(frame)
            if closed.empty or _naive(closed["datetime"].max()) < expected:
                return False
        return True

    # ── внутренние помощники ──
    def _initial_load(self, instrument, key: tuple) -> None:
        df = self._load(instrument, start_date=None)
        self._frames[key] = df if df is not None else None
        self._instruments[key] = instrument
        self._observed[key] = _naive(self._timeline.current_candle_start(self._timeline.now()))
        if df is not None and not df.empty:
            closed = self._closed_only(df)
            if not closed.empty:
                self._last_loaded[key] = _naive(closed["datetime"].max())

    def _closed_only(self, frame: pd.DataFrame) -> pd.DataFrame:
        boundary = _naive(self._timeline.current_candle_start(self._timeline.now()))
        return frame[frame["datetime"] < boundary].copy()

    def _merge_new_bars(self, frame, new_df, last_dt):
        if new_df is None or new_df.empty:
            return frame
        if last_dt is not None:
            new_df = new_df[new_df["datetime"] > _naive(last_dt)]
        if new_df.empty:
            return frame
        return pd.concat([frame, new_df], ignore_index=True).drop_duplicates(
            subset="datetime", keep="last"
        ).sort_values("datetime").reset_index(drop=True)
