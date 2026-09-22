from __future__ import annotations

import time

import pandas as pd

from src.api.retry import DEFAULT_BASE_DELAY, rate_limit_reset_secs
from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)


def _naive(dt) -> pd.Timestamp:
    """Делегирует единому helper: приводит время к tz-naive pandas.Timestamp (UTC без пояса).

    Сохраняется как защита на границах готовности свечи: вход уже naive (no-op),
    но при возможном рецидиве aware-времени гарантирует целостность сравнений.
    """
    return to_naive(dt)


class MarketDataCache:
    """Кэш истории свечей: дозагрузка только новых закрытых баров, отдача закрытых свечей.

    Лениво загружает первый срез по паре (инструмент, таймфрейм), отдаёт стратегиям
    только готовые (закрытые) свечи и при появлении нового закрытого бара
    инкрементально дозагружает новые бары поверх кэша. Кадры разных таймфреймов
    одного инструмента хранятся и обновляются независимо.
    """

    def __init__(self, loader, timeline, token=None, data_refresh_min_interval=0.0, data_backfill_window_seconds=None, freshness_tolerance_bars=0) -> None:
        self._loader = loader
        self._timeline = timeline  # MultiTimeframeScheduler: сетки и рыночное время
        self._token = token
        self._data_refresh_min_interval = data_refresh_min_interval  # мин. пауза между API-дозагрузками
        self._data_backfill_window_seconds = data_backfill_window_seconds  # окно инкр. дозагрузки (bounded backfill)
        self._freshness_tolerance_bars = freshness_tolerance_bars  # терпимость готовности ТФ (в барах), 0 = жёсткий AND
        self._last_api_attempt: pd.Timestamp | None = None  # глобальный страж последнего обращения к API (tz-naive UTC)
        self._retry_after: pd.Timestamp | None = None  # до этого времени дозагрузки приостановлены (tz-naive)
        self._frames: dict[tuple, pd.DataFrame | None] = {}
        self._instruments: dict[tuple, object] = {}
        self._last_loaded: dict[tuple, pd.Timestamp] = {}
        self._observed: dict[tuple, pd.Timestamp] = {}
        self._uids: dict[tuple, str] = {}

    def _key(self, instrument, timeframe: str) -> tuple:
        return (instrument.ticker, instrument.instrument_type, timeframe)

    def _load(self, instrument, timeframe: str, start_date=None) -> pd.DataFrame:
        key = self._key(instrument, timeframe)
        self._last_api_attempt = _naive(self._timeline.now())
        df, instrument_id = self._loader(
            ticker=instrument.ticker,
            instrument_type=instrument.instrument_type,
            timeframe=timeframe,
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

    def frame_for(self, instrument, timeframe: str) -> pd.DataFrame:
        """Готовые (закрытые) свечи пары (инструмент, ТФ); ленивая первичная загрузка."""
        key = self._key(instrument, timeframe)
        if key not in self._frames:
            self._initial_load(instrument, timeframe, key)
        frame = self._frames[key]
        if frame is None or frame.empty:
            return pd.DataFrame()
        return self._closed_only(frame, timeframe)

    def ensure_loaded(self, instrument, timeframe: str) -> None:
        """Гарантирует актуальность кадра пары (инструмент, ТФ) по требованию.

        Используется для таймфреймов вне активного ритма (старшие ТФ фильтров):
        отсутствующий кадр загружается целиком, существующий — инкрементально
        дозагружается новыми закрытыми барами поверх кэша.
        """
        key = self._key(instrument, timeframe)
        if key not in self._frames:
            self._initial_load(instrument, timeframe, key)
            return
        frame = self._frames[key]
        if frame is None or frame.empty:
            return
        now = _naive(self._timeline.now())
        if self._retry_after is not None:
            if now < self._retry_after:
                return
            self._retry_after = None
        if self._throttled(now):
            return
        last_dt = self._last_loaded.get(key)
        start = self._incremental_start(last_dt, now)
        try:
            new_df = self._load(self._instruments[key], timeframe, start_date=start)
        except Exception as exc:
            if "resource_exhausted" in str(exc).lower():
                log.warning("Rate limit при дозагрузке %s: %s", key, exc)
                self._retry_after = self._pause_after_rate_limit(exc, now)
                return
            raise
        merged = self._merge_new_bars(frame, new_df, last_dt)
        self._frames[key] = merged
        closed = self._closed_only(merged, timeframe)
        if not closed.empty:
            self._last_loaded[key] = _naive(closed["datetime"].max())

    def refresh_if_new_candle(self, timeframe: str, now=None, force: bool = False) -> None:
        """Инкрементально дозагружает новые закрытые бары таймфрейма, если граница сместилась.

        ``force=True`` заставляет повторно дозагружать поверх кэша даже если граница
        уже отслежена (используется при ожидании появления свежего закрытого бара,
        который из-за задержки публикации может быть временно недоступен).

        Окно ``data_refresh_min_interval`` распределяется между кадрами таймфрейма
        честно: кадры упорядочиваются по устареванию последнего закрытого бара
        (самый отсталый — первый) и за один вызов дозагружается только один кадр.
        Кадр, пропущенный из-за интервального лимита, НЕ помечается обновлённым —
        он остаётся кандидатом следующего окна, так что ни один кадр с данными не
        «голодает».
        """
        grid = self._timeline.grid(timeframe)
        now = _naive(now or self._timeline.now())
        boundary = _naive(grid.current_candle_start(now))
        if self._retry_after is not None:
            if now < self._retry_after:
                return
            self._retry_after = None
        candidates = []
        for key in list(self._frames.keys()):
            if key[2] != timeframe:
                continue
            if not force and self._observed.get(key, boundary) >= boundary:
                continue
            frame = self._frames[key]
            if frame is None or frame.empty:
                self._observed[key] = boundary
                continue
            closed = self._closed_only(frame, timeframe)
            staleness = _naive(closed["datetime"].max()) if not closed.empty else pd.Timestamp.min
            candidates.append((staleness, key))
        candidates.sort(key=lambda item: (item[0], item[1]))
        for _, key in candidates:
            if self._throttled(now):
                return
            frame = self._frames[key]
            last_dt = self._last_loaded.get(key)
            start = self._incremental_start(last_dt, now)
            try:
                new_df = self._load(self._instruments[key], timeframe, start_date=start)
            except Exception as exc:
                if "resource_exhausted" in str(exc).lower():
                    log.warning("Rate limit при дозагрузке %s: %s", key, exc)
                    self._retry_after = self._pause_after_rate_limit(exc, now)
                    return
                raise
            merged = self._merge_new_bars(frame, new_df, last_dt)
            self._frames[key] = merged
            closed = self._closed_only(merged, timeframe)
            self._last_loaded[key] = _naive(closed["datetime"].max()) if not closed.empty else last_dt
            self._observed[key] = boundary

    def has_fresh_closed_bar(self, timeframe: str, now=None) -> bool:
        """Появился ли свежий закрытый бар таймфрейма в загруженных кэшах.

        Ожидаемый самый свежий закрытый бар начинается в
        ``previous_candle_start`` (``current_candle_start - period``) сетки этого ТФ.

        При ``freshness_tolerance_bars > 0`` пары «инструмент — ТФ», чей последний
        закрытый бар старше ``expected - tol`` (``tol = бара × период``),
        считаются неактивными (неликвидные/отстающие) и исключаются из гейта:
        они не блокируют готовые пары и тик. Дальше гейт требует готовности
        каждой активной пары; если активных пар нет вовсе — ``False`` (свежесть
        не выдумывается при отключённом фиде). Кадров этого ТФ нет → ``True``.
        При ``freshness_tolerance_bars == 0`` (по умолчанию) — прежний жёсткий
        AND: любая пара без свежего бара возвращает ``False``.
        """
        grid = self._timeline.grid(timeframe)
        now = _naive(now or self._timeline.now())
        expected = _naive(grid.previous_candle_start(now))
        if self._freshness_tolerance_bars <= 0:
            frames = [
                frame
                for key, frame in self._frames.items()
                if key[2] == timeframe
            ]
            if not frames:
                return True
            for frame in frames:
                if frame is None or frame.empty:
                    continue
                closed = self._closed_only(frame, timeframe)
                if closed.empty or _naive(closed["datetime"].max()) < expected:
                    return False
            return True

        period_secs = self._tf_period_secs(timeframe, now)
        tol = pd.Timedelta(seconds=self._freshness_tolerance_bars * period_secs)
        threshold = expected - tol
        active = 0
        lagging = False
        have_frames = False
        for key, frame in self._frames.items():
            if key[2] != timeframe:
                continue
            have_frames = True
            if frame is None or frame.empty:
                continue
            closed = self._closed_only(frame, timeframe)
            if closed.empty:
                continue
            last = _naive(closed["datetime"].max())
            if last < threshold:
                log.debug(
                    "Пара %s исключена из гейта ТФ %s: последний бар %s старше допуска %s",
                    key, timeframe, last, threshold,
                )
                continue
            active += 1
            if last < expected:
                lagging = True
        if not have_frames:
            return True
        if active == 0:
            return False
        return not lagging

    def _tf_period_secs(self, timeframe: str, now=None) -> float:
        """Длительность периода ТФ (в секундах) на сетке таймфрейма."""
        grid = self._timeline.grid(timeframe)
        current = _naive(now or self._timeline.now())
        return (grid.next_candle_close(current) - grid.current_candle_start(current)).total_seconds()

    # ── внутренние помощники ──
    def _initial_load(self, instrument, timeframe: str, key: tuple) -> None:
        self._wait_throttle()
        df = self._load(instrument, timeframe, start_date=None)
        self._frames[key] = df if df is not None else None
        self._instruments[key] = instrument
        grid = self._timeline.grid(timeframe)
        self._observed[key] = _naive(grid.current_candle_start(self._timeline.now()))
        if df is not None and not df.empty:
            closed = self._closed_only(df, timeframe)
            if not closed.empty:
                self._last_loaded[key] = _naive(closed["datetime"].max())

    def _wait_throttle(self) -> None:
        """Досыпает остаток ``data_refresh_min_interval`` с последнего API-вызова.

        Разносит стартовые/восстановительные загрузки кадров, чтобы не выжигать
        лимит запросов. ``_load`` по-прежнему обновляет ``_last_api_attempt``.
        """
        if not self._data_refresh_min_interval or self._last_api_attempt is None:
            return
        elapsed = (_naive(self._timeline.now()) - self._last_api_attempt).total_seconds()
        remaining = self._data_refresh_min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _closed_only(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        grid = self._timeline.grid(timeframe)
        boundary = _naive(grid.current_candle_start(self._timeline.now()))
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

    def _throttled(self, now) -> bool:
        """Дозагрузка запрещена троттлингом: с последнего API-вызова прошло меньше интервала."""
        if not self._data_refresh_min_interval or self._last_api_attempt is None:
            return False
        return (now - self._last_api_attempt).total_seconds() < self._data_refresh_min_interval

    def _incremental_start(self, last_dt, now):
        """start_date дозагрузки: не раньше окна bounded backfill (``now - window``)."""
        if self._data_backfill_window_seconds is None:
            return last_dt
        window_start = now - pd.Timedelta(seconds=self._data_backfill_window_seconds)
        if last_dt is None:
            return window_start
        return max(window_start, _naive(last_dt))

    def _pause_after_rate_limit(self, exc, now) -> pd.Timestamp:
        """Момент возобновления дозагрузок после RESOURCE_EXHAUSTED (tz-naive wall-time)."""
        reset = rate_limit_reset_secs(exc) or 0
        pause = max(reset, self._data_refresh_min_interval or DEFAULT_BASE_DELAY)
        return now + pd.Timedelta(seconds=pause)
