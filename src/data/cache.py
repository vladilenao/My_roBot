from __future__ import annotations

import time
import threading
from collections import deque
from functools import wraps
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from src.api.retry import DEFAULT_BASE_DELAY, rate_limit_reset_secs
from src.data.timeutil import to_naive
from src.instruments.model import instrument_key
from src.history.source import source_identity
from src.logging_setup import get_logger
from src.data.request_budget import RequestDeferred

log = get_logger(__name__)


def _synchronized(method):
    """Защищает кадры при фоновом приёме и основном торговом цикле."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped

# Сколько тиков подряд без единого нового закрытого бара — кандидат в разрыв
# данных. Дальше решает поиск следующего имеющегося бара: нашёлся — это пауза в
# данных и прогон идёт дальше, не нашёлся — конец доступных данных. Считается
# раз в рыночный тик, а не раз в вызов на таймфрейм (см. `close_tick`).
CONSECUTIVE_EMPTY_TICKS_BEFORE_END = 2

# Насколько далеко от текущего рыночного момента искать следующий имеющийся бар.
# Верхняя граница для пауз между сессиями, выходных и праздников; разрыв длиннее
# недели — это уже обрыв среза, и такой прогон честно останавливается с недобором.
GAP_SEARCH_LOOKAHEAD = timedelta(days=7)


def _naive_series(stamps: pd.Series) -> pd.Series:
    """Векторный аналог ``_naive`` для целой колонки ``datetime``.

    Часовой пояс срезается, если он есть; naive-колонка возвращается как есть.
    """
    if isinstance(stamps.dtype, pd.DatetimeTZDtype):
        return stamps.dt.tz_localize(None)
    return stamps


def _naive(dt) -> pd.Timestamp:
    """Делегирует единому helper: приводит время к tz-naive pandas.Timestamp (UTC без пояса).

    Сохраняется как защита на границах готовности свечи: вход уже naive (no-op),
    но при возможном рецидиве aware-времени гарантирует целостность сравнений.
    """
    return to_naive(dt)


@dataclass
class DataGap:
    """Разрыв в данных: промежуток рыночного времени без ожидаемых баров.

    ``since`` — рыночный момент, с которого прогона ждёт бар, которого нет;
    ``resume`` — момент ближайшего имеющегося бара, с которого данные возобновляются;
    ``missed`` — сколько баров мельчайшего активного таймфрейма пропало за разрыв;
    ``span`` — длительность промежутка отсутствия баров (от баром после последнего
    имеющегося до следующего имеющегося), в отчёт идёт максимум этих значений.
    """

    since: pd.Timestamp
    resume: pd.Timestamp
    missed: int
    span: pd.Timedelta = pd.Timedelta(0)
    last_bar: pd.Timestamp | None = None
    empty_ticks: int = 0


@dataclass
class DataExhaustion:
    """Что именно не нашёл кэш, объявив конец доступных данных.

    ``at`` — рыночный момент, с которого искали бар; ``horizon_start`` и
    ``horizon_end`` — границы реально проверенного окна поиска
    ``[at, min(at + 7 суток, конец диапазона)]``. ``horizon_limited`` равен
    ``True``, когда поиск оборван семью сутками раньше конца диапазона: в этом
    случае кэш НЕ утверждает, что баров после проверенного участка нет.
    """

    at: pd.Timestamp
    horizon_start: pd.Timestamp
    horizon_end: pd.Timestamp
    range_end: pd.Timestamp | None = None
    horizon_limited: bool = False


@dataclass(frozen=True)
class TerminalGap:
    end: pd.Timestamp
    last_processed_bar: pd.Timestamp
    checks: tuple


class MarketDataCache:
    """Кэш истории свечей: дозагрузка только новых закрытых баров, отдача закрытых свечей.

    Лениво загружает первый срез по паре (инструмент, таймфрейм), отдаёт стратегиям
    только готовые (закрытые) свечи и при появлении нового закрытого бара
    инкрементально дозагружает новые бары поверх кэша. Кадры разных таймфреймов
    одного инструмента хранятся и обновляются независимо.

    На виртуальных часах (исторический прогон) повторные принудительные
    загрузки и паузы между ними отключены: отсутствие бара на текущем рыночном
    моменте не означает конец доступных данных — это разрыв в данных (перерыв
    между сессиями, выходной, праздник). Такой разрыв кэш находит поиском
    следующего имеющегося бара и сообщает прогону вместе со статистикой
    (количество разрывов, пропущенные бары, самый длинный разрыв). Конец данных
    сигнализируется только когда следующего бара нет.
    """

    #: Конец доступных данных: устанавливается, когда новые бары перестали приходить
    #: и следующего имеющегося бара в горизонте поиска нет.
    data_exhausted: bool = False

    #: Границы проверенного горизонта поиска в момент объявления конца данных;
    #: ``None``, пока данных не объявлено конец.
    data_exhaustion: DataExhaustion | None = None

    def __init__(self, loader, timeline, token=None, data_refresh_min_interval=0.0, data_backfill_window_seconds=None, freshness_tolerance_bars=0, clock=None, client_provider=None, market_store=None, market_source="tbank_exchange", market_reconciler=None, request_budget=None, recovery_retry_seconds=65) -> None:
        self._loader = loader
        self._timeline = timeline  # MultiTimeframeScheduler: сетки и рыночное время
        self._token = token
        self._clock = clock
        self._history = bool(getattr(clock, "is_virtual", False))
        self._client_provider = client_provider
        # В историческом режиме источник уже является эмулятором. Локальный
        # журнал живых данных включается только вызывающим кодом live-режима.
        self._market_store = market_store
        self._market_source = market_source
        self._market_reconciler = market_reconciler
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
        self._seen: dict[tuple, pd.Timestamp | None] = {}
        self._missed_bars = 0
        self._gaps = 0
        self._longest_gap = pd.Timedelta(0)
        self._gap: DataGap | None = None  # открытый разрыв (данные ещё не возобновились)
        self._pending_gap: DataGap | None = None  # найденный разрыв, ждущий перевода рыночного времени
        self._empty_ticks = 0
        self._tick_progressed = 0
        self._tick_expected: set[str] = set()
        self._lock = threading.RLock()
        self._coverage_blocks: set[tuple] = set()
        self._active_tickers = None
        self._active_pairs = None
        self._next_contract_event = None
        self._pending_calendar = None
        self._boundary_checks = ()
        self._processed_drivers = {}
        self._pending_terminal_gap = None
        self._terminal_gap_counted = False
        self._failed_reads = set()
        self._request_budget = request_budget
        self.live_session_enabled = request_budget is not None and not self._history
        self._live_queue = deque()
        self._recovery_queue = deque()
        self._live_busy = set()
        self._refresh_running = False
        self._recovery_due = False
        self._recovery_retry_seconds = max(recovery_retry_seconds, data_refresh_min_interval)
        self._aux_at = pd.Timestamp.min
        self._live_errors = {}
        self._warmup_days = {}

    @property
    def history_source_failed(self):
        return bool(self._failed_reads)

    def set_boundary_checks(self, checks):
        self._boundary_checks = tuple(checks)

    @property
    def last_processed_bar(self):
        return max(self._processed_drivers.values(), default=None)

    def note_driver_processed(self):
        """Вызывается после обработки тика, а не при прогреве/чтении свечей."""
        if not self._history:
            return
        tf = self._driver_timeframe()
        for key, frame in self._frames.items():
            if key[2] != tf or not self._active_key(key) or frame is None or frame.empty:
                continue
            closed = self._closed_only(frame, tf)
            closed = closed.loc[(closed["datetime"] >= self._clock.start) & (closed["datetime"] < self._clock.end)]
            if closed.empty:
                continue
            uid = self._uids.get(key) or getattr(self._instruments[key], "uid", None)
            identity = (uid, key[1], key[2])
            self._processed_drivers[identity] = _naive(closed["datetime"].max())

    def take_terminal_gap(self):
        gap, self._pending_terminal_gap = self._pending_terminal_gap, None
        return gap

    def _end_driver_checks(self):
        if not self._boundary_checks or self._failed_reads:
            return ()
        end, tf = self._history_range_end(), self._driver_timeframe()
        if end is None or tf is None:
            return ()
        source, snapshot = source_identity(self._client_provider)
        checks = [item for item in self._boundary_checks if item.role == "end" and item.boundary == end and item.pair.timeframe == tf]
        indexed = {(item.pair.instrument_id, item.pair.instrument_type, tf): item for item in checks}
        active = {(self._uids.get(key) or getattr(instrument, "uid", None), key[1], tf)
                  for key, instrument in self._instruments.items() if key[2] == tf and self._active_key(key)}
        required = set(indexed) | active
        if not required:
            return ()
        for identity in required:
            item = indexed.get(identity)
            if item is None or not item.ok or item.source_id != source or item.snapshot_generation != snapshot or not identity[0]:
                return ()
            processed = self._processed_drivers.get(identity)
            if processed is None or item.last_before is None or processed < item.last_before:
                return ()
        return tuple(checks)

    def final_drivers_processed(self):
        return not self._boundary_checks or bool(self._end_driver_checks())

    def _confirm_terminal_gap(self, now):
        if self._terminal_gap_counted:
            return False
        checks = self._end_driver_checks()
        if not checks or not any(item.status == "confirmed_gap" for item in checks):
            return False
        end, tf = self._history_range_end(), self._driver_timeframe()
        # Все возможные события до end должны сначала пройти обычный контур.
        event = self._next_contract_event(now) if self._next_contract_event is not None else None
        if event is not None and now < _naive(event) < _naive(end):
            return False
        last = max(_naive(item.last_before) for item in checks)
        period = pd.Timedelta(seconds=self._tf_period_secs(tf))
        lower = last+period
        if now < lower:
            return False
        span = max(pd.Timedelta(0), _naive(end)-lower)
        if span > pd.Timedelta(0):
            self._missed_bars += int(-(-span.value // period.value))
            self._gaps += 1
            self._longest_gap = max(self._longest_gap, span)
        self._gap = None
        self._empty_ticks = 0
        self._terminal_gap_counted = True
        self._pending_terminal_gap = TerminalGap(_naive(end), last, tuple(checks))
        return True

    def set_active_instruments(self, instruments, *, next_event=None, active_pairs=None):
        self._active_tickers = {instrument_key(item) for item in instruments}
        self._active_pairs = active_pairs
        self._next_contract_event = next_event

    def _active_key(self, key):
        return (self._active_tickers is None or key[0] in self._active_tickers) and (
            self._active_pairs is None or (key[0], key[2]) in self._active_pairs)

    def instrument_for(self, ticker):
        return next(instrument for instrument in self._instruments.values() if instrument_key(instrument) == ticker)

    def take_calendar_event(self):
        event, self._pending_calendar = self._pending_calendar, None
        return event

    @_synchronized
    def prepare_history(self, instrument, timeframe, min_bars, earliest):
        """Собственные закрытые бары; служебное чтение не двигает исполнение."""
        now = _naive(self._timeline.now())
        if now <= earliest:
            return False
        self.ensure_loaded(instrument, timeframe)
        frame = self.frame_for(instrument, timeframe)
        span = pd.Timedelta(days=30)
        while len(frame) < min_bars:
            span *= 2
            lower = max(_naive(earliest), now-span)
            key = self._key(instrument, timeframe)
            loaded = self._load(instrument, timeframe, start_date=lower)
            self._frames[key] = loaded
            frame = self.frame_for(instrument, timeframe)
            if not frame.empty:
                self._last_loaded[key] = _naive(frame["datetime"].max())
            if lower == _naive(earliest):
                break
        return len(frame) >= min_bars

    @property
    def missed_bars(self) -> int:
        """Сколько ожидаемых баров внутри диапазона не появилось (для отчёта)."""
        return self._missed_bars

    @property
    def gaps(self) -> int:
        """Сколько разрывов в данных пережил прогон (для отчёта)."""
        return self._gaps

    @property
    def longest_gap(self) -> pd.Timedelta:
        """Длительность самого длинного разрыва в данных (для отчёта)."""
        return self._longest_gap

    def take_pending_gap(self) -> DataGap | None:
        """Забирает найденный разрыв, если он есть, и снимает его с учёта.

        Разрыв приходит сюда один раз: кэш сообщает прогону момент следующего
        имеющегося бара, а рыночным временем распоряжается уже сессия прогона.
        """
        gap, self._pending_gap = self._pending_gap, None
        return gap

    def _key(self, instrument, timeframe: str) -> tuple:
        return (instrument_key(instrument), instrument.instrument_type, timeframe)

    def _source_kwargs(self) -> dict:
        """Аргументы источника данных для загрузчика.

        Боевой режим ничего не добавляет: загрузчик сам берёт системное время и
        провайдер по умолчанию. Исторический прогон передаёт свой источник и
        виртуальные часы, иначе границы дозагрузки считались бы от «сейчас».
        """
        if not self._history:
            return {}
        return {"client_provider": self._client_provider, "clock": self._clock}

    def _load(self, instrument, timeframe: str, start_date=None, end_date=None, *, wait_for_budget=True) -> pd.DataFrame:
        """Дозагрузка кадра по окну ``[start_date, end_date)``.

        Обычная дозагрузка ``end_date`` не задаёт: в историческом прогоне загрузчик
        сам ограничивает срез текущим рыночным моментом. Явное ``end_date`` нужно
        только поиску следующего бара за разрывом — там окно ограничено горизонтом
        поиска, а не рыночным временем.
        """
        key = self._key(instrument, timeframe)
        self._last_api_attempt = _naive(self._timeline.now())
        if self._history and (end_date is not None or _naive(self._timeline.now()) > _naive(self._clock.end)):
            end_date = min(_naive(end_date or self._timeline.now()), _naive(self._clock.end))
        try:
            df, instrument_id = self._loader(
                ticker=instrument.ticker,
                instrument_type=instrument.instrument_type,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                token=self._token,
                instrument_id=self._uids.get(key) or getattr(instrument, "uid", None),
                **({"request_budget": self._request_budget, "wait_for_budget": wait_for_budget,
                    "single_attempt": True, "client_provider": self._client_provider,
                    "clock": self._timeline.clock} if self.live_session_enabled else {}),
                **self._source_kwargs(),
            )
        except Exception:
            if self._history:
                self._failed_reads.add(key)
            raise
        self._failed_reads.discard(key)
        if instrument_id is not None:
            self._uids[key] = instrument_id
        if df is not None and not df.empty and "datetime" in df.columns:
            if self._history:
                df = df.copy()
                df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(None)
                df = df.loc[df["datetime"] < _naive(self._clock.end)]
            df = df.sort_values("datetime").reset_index(drop=True)
            if self._market_store is not None and instrument_id:
                self._market_store.record_candles(
                    source=self._market_source,
                    instrument_uid=instrument_id,
                    interval=timeframe,
                    candles=df,
                    received_at=self._timeline.now(),
                )
                # Стратегия читает тот же подтверждённый локальный слепок,
                # который экспортируется эмулятору. Это позволяет пережить
                # перезапуск и принять позднюю исправленную минуту.
                df = self._market_store.current_frame(
                    source=self._market_source,
                    instrument_uid=instrument_id,
                    interval=timeframe,
                )
                if self._market_reconciler is not None:
                    self._market_reconciler.reconcile(
                        instrument_uid=instrument_id, interval=timeframe,
                        candles=df, now=self._timeline.now(),
                    )
        return df

    def frame_for(self, instrument, timeframe: str) -> pd.DataFrame:
        if not self.live_session_enabled:
            return self._frame_for_legacy(instrument, timeframe)
        self._live_initial(instrument, timeframe)
        with self._lock:
            frame = self._frames.get(self._key(instrument, timeframe))
            if frame is None or frame.empty:
                return pd.DataFrame()
            closed = self._closed_only(frame, timeframe)
        # Получение не обрезается из-за старой дыры; исполнительный барьер
        # проверяет contiguous_after, анализ — coverage_for необходимого хвоста.
        return closed

    @_synchronized
    def _frame_for_legacy(self, instrument, timeframe: str) -> pd.DataFrame:
        """Готовые (закрытые) свечи пары (инструмент, ТФ); ленивая первичная загрузка."""
        key = self._key(instrument, timeframe)
        if key not in self._frames:
            self._initial_load(instrument, timeframe, key)
        frame = self._frames[key]
        if frame is None or frame.empty:
            return pd.DataFrame()
        closed = self._closed_only(frame, timeframe)
        return self._resolved_only(key, closed, timeframe)

    @_synchronized
    def contiguous_after(self, instrument, timeframe: str, frame: pd.DataFrame, last_bar) -> pd.DataFrame:
        """Оставляет для симуляции ряд до первой неразрешённой минуты.

        ``frame_for`` защищает анализ от внутренних дыр, а этот метод учитывает
        ещё и already durable курсор исполнения, который может быть раньше
        первого нового ряда после перезапуска.
        """
        if self._market_reconciler is None or frame.empty or timeframe != "1m" or last_bar is None:
            return frame
        key = self._key(instrument, timeframe)
        instrument_id = self._uids.get(key)
        if not instrument_id:
            return frame.iloc[0:0].copy()
        previous = _naive(last_bar)
        allowed: list[int] = []
        for index, row in frame.sort_values("datetime").iterrows():
            stamp = _naive(row["datetime"])
            probe = previous + pd.Timedelta(minutes=1)
            while probe < stamp:
                state = self._market_reconciler.interval_state(
                    instrument_uid=instrument_id, interval=timeframe,
                    open_time=probe, now=self._timeline.now(),
                )
                if state not in {"received", "scheduled_closed", "confirmed_no_trade"}:
                    self._report_coverage_block(key, probe, state)
                    return frame.loc[allowed].copy()
                probe += pd.Timedelta(minutes=1)
            allowed.append(index)
            previous = stamp
        return frame.loc[allowed].copy()

    @_synchronized
    def mark_processed(self, instrument, timeframe: str, stamp) -> None:
        """Фиксирует прогресс симуляции только после commit журнала сделок."""
        if self._market_store is None:
            return
        instrument_id = self._uids.get(self._key(instrument, timeframe))
        if instrument_id:
            self._market_store.mark_processed(
                source=self._market_source, instrument_uid=instrument_id,
                interval=timeframe, open_time=stamp,
            )

    def ensure_loaded(self, instrument, timeframe: str) -> None:
        if not self.live_session_enabled:
            return self._ensure_loaded_legacy(instrument, timeframe)
        self._live_initial(instrument, timeframe)
        self.refresh_live()

    @_synchronized
    def _ensure_loaded_legacy(self, instrument, timeframe: str) -> None:
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
            if self._history:
                self._initial_load(instrument, timeframe, key)
            return
        now = _naive(self._timeline.now())
        if not self._history:
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
            if not self._history and "resource_exhausted" in str(exc).lower():
                log.warning("Rate limit при дозагрузке %s: %s", key, exc)
                self._retry_after = self._pause_after_rate_limit(exc, now)
                return
            raise
        merged = self._merge_new_bars(frame, new_df, last_dt)
        self._frames[key] = merged
        closed = self._closed_only(merged, timeframe)
        if not closed.empty:
            self._last_loaded[key] = _naive(closed["datetime"].max())
            self._note_new_bar(key, closed)

    def refresh_if_new_candle(self, timeframe: str, now=None, force: bool = False) -> None:
        if self.live_session_enabled:
            return self.refresh_live()
        return self._refresh_legacy(timeframe, now, force)

    @_synchronized
    def _refresh_legacy(self, timeframe: str, now=None, force: bool = False) -> None:
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
        if self._history and self.data_exhausted:
            return
        if not self._history and self._retry_after is not None:
            if now < self._retry_after:
                return
            self._retry_after = None
        candidates = []
        for key in list(self._frames.keys()):
            if key[2] != timeframe or not self._active_key(key):
                continue
            if not force and self._observed.get(key, boundary) >= boundary:
                continue
            frame = self._frames[key]
            if frame is None or frame.empty:
                if self._history:
                    self._initial_load(self._instruments[key], timeframe, key)
                    frame = self._frames[key]
                    if frame is not None and not frame.empty:
                        candidates.append((pd.Timestamp.min, key))
                self._observed[key] = boundary
                continue
            closed = self._closed_only(frame, timeframe)
            staleness = _naive(closed["datetime"].max()) if not closed.empty else pd.Timestamp.min
            candidates.append((staleness, key))
        candidates.sort(key=lambda item: (item[0], item[1]))
        progressed = 0
        for _, key in candidates:
            if not self._history and self._throttled(now):
                return
            frame = self._frames[key]
            last_dt = self._last_loaded.get(key)
            recovered, recovery_attempted = self._recover_oldest_gap(key, timeframe, now)
            if recovery_attempted:
                merged = self._merge_new_bars(frame, recovered, last_dt)
            else:
                start = self._incremental_start(last_dt, now)
                try:
                    new_df = self._load(self._instruments[key], timeframe, start_date=start)
                except Exception as exc:
                    if not self._history and "resource_exhausted" in str(exc).lower():
                        log.warning("Rate limit при дозагрузке %s: %s", key, exc)
                        self._retry_after = self._pause_after_rate_limit(exc, now)
                        return
                    raise
                merged = self._merge_new_bars(frame, new_df, last_dt)
            self._frames[key] = merged
            closed = self._closed_only(merged, timeframe)
            self._last_loaded[key] = _naive(closed["datetime"].max()) if not closed.empty else last_dt
            self._observed[key] = boundary
            if self._note_new_bar(key, closed):
                progressed += 1
        if self._history:
            self._collect_tick(timeframe, progressed, len(candidates))

    def _live_initial(self, instrument, timeframe):
        key = self._key(instrument, timeframe)
        with self._lock:
            if key in self._frames or key in self._live_busy:
                return
            self._instruments[key] = instrument
            self._frames[key] = pd.DataFrame()
            self._live_busy.add(key)
        try:
            uid = getattr(instrument, "uid", None) or self._uids.get(key)
            if uid is None and self._market_store is not None and self.live_session_enabled:
                uid = self._resolve_uid(instrument)
            if self._market_store is not None and uid:
                stored = self._market_store.current_frame(source=self._market_source,
                    instrument_uid=uid, interval=timeframe)
                self._uids[key] = uid
                self._live_merge(key, stored)
                if not stored.empty:
                    return
            self._live_merge(key, self._load(instrument, timeframe))
            self._warmup_days[key] = 30
        except Exception as exc:
            self._live_errors[key] = str(exc)
            raise
        finally:
            with self._lock:
                self._live_busy.discard(key)

    def _resolve_uid(self, instrument):
        """UID из каталога без стрима свечей: нужен, чтобы читать прогрев из базы."""
        from src.data.loader import resolve_instrument_id
        try:
            return resolve_instrument_id(
                ticker=instrument.ticker, instrument_type=instrument.instrument_type,
                token=self._token, client_provider=self._client_provider,
                request_budget=self._request_budget, clock=self._timeline.clock,
            )
        except RequestDeferred:
            return None
        except Exception as exc:
            log.warning("Не удалось узнать UID %s из каталога: %s", instrument.ticker, exc)
            return None

    def _live_merge(self, key, incoming):
        with self._lock:
            frame = self._frames.get(key)
            if frame is None or frame.empty:
                frame = incoming if incoming is not None else pd.DataFrame()
            else:
                frame = self._merge_new_bars(frame, incoming, self._last_loaded.get(key))
            self._frames[key] = frame
            if not frame.empty:
                closed = self._closed_only(frame, key[2])
                if not closed.empty:
                    self._last_loaded[key] = _naive(closed["datetime"].max())
                    self._note_new_bar(key, closed)
            self._observed[key] = _naive(self._timeline.grid(key[2]).current_candle_start(self._timeline.now()))

    def refresh_live(self):
        """Один свежий dispatch; после оборота — одна auxiliary/recovery задача."""
        if not self.live_session_enabled:
            return
        now = _naive(self._timeline.now())
        with self._lock:
            if self._refresh_running or self._throttled(now):
                return
            if self._request_budget.retry_after is not None and now < self._request_budget.retry_after:
                return
            if not self._live_queue:
                candidates = []
                for key, frame in self._frames.items():
                    if not self._active_key(key) or key in self._live_busy:
                        continue
                    last = self._last_loaded.get(key, pd.Timestamp.min)
                    expected = self._expected_live_open(key, now)
                    if expected is None:
                        continue
                    if last >= expected:
                        continue
                    candidates.append((last, key))
                self._live_queue.extend(key for _, key in sorted(candidates))
            auxiliary = self._recovery_due or (not self._live_queue and now >= self._aux_at)
            if auxiliary:
                self._recovery_due = False
                self._aux_at = now + pd.Timedelta(seconds=self._recovery_retry_seconds)
                self._live_busy.add(("aux",))
                key = None
            elif self._live_queue:
                key = self._live_queue.popleft()
                if key in self._live_busy:
                    self._live_queue.append(key)
                    return
                self._live_busy.add(key)
            else:
                return
            self._refresh_running = True
        try:
            if auxiliary:
                self._service_auxiliary(now)
                return
            if not self._active_key(key):
                return
            expected = self._expected_live_open(key, now)
            if expected is None:
                return
            # Скользящее окно в час не должно исключать последнюю H1-свечу
            # (в 09:30 её open_time=08:00, а now-window=08:30).
            start = min(self._incremental_start(self._last_loaded.get(key), now), expected)
            calendar = self.calendar_for(self._instruments[key])
            end = self._timeline.grid(key[2]).bar_close(expected) if (
                calendar is not None and calendar.covers(now) and not calendar.is_open(now)) else None
            incoming = self._load(self._instruments[key], key[2], start_date=start,
                                  end_date=end, wait_for_budget=False)
            self._live_merge(key, incoming)
            self._live_errors.pop(key, None)
        except RequestDeferred:
            with self._lock:
                if key is not None:
                    self._live_queue.appendleft(key)
        except Exception as exc:
            if key is not None:
                self._live_errors[key] = str(exc)
            log.warning("Обновление рыночных данных %s: %s", key, exc)
        finally:
            with self._lock:
                self._live_busy.discard(key if key is not None else ("aux",))
                self._refresh_running = False
                if key is not None and not self._live_queue:
                    self._recovery_due = True

    def _expected_live_open(self, key, now):
        grid = self._timeline.grid(key[2])
        expected = _naive(grid.previous_candle_start(now))
        reconciler = self._market_reconciler
        uid = self._uids.get(key)
        cal = reconciler.session_calendar(uid, now) if uid and reconciler else None
        if cal is None or not cal.covers(now):
            return expected
        starts = []
        for lower, upper in cal.intervals:
            if lower > now:
                continue
            stamp = min(expected, _naive(grid.current_candle_start(upper - pd.Timedelta(microseconds=1))))
            if grid.bar_close(stamp) <= now and grid.bar_close(stamp) > lower:
                starts.append(stamp)
        return max(starts, default=None)

    def _service_auxiliary(self, now):
        reconciler = self._market_reconciler
        if reconciler is None:
            return
        # Календарь и recovery чередуются, чтобы один вид не голодал.
        if not hasattr(self, "_aux_calendar_turn"):
            self._aux_calendar_turn = True
        calendar_turn = self._aux_calendar_turn
        self._aux_calendar_turn = not calendar_turn
        if calendar_turn and reconciler.service_calendar(now):
            return
        with self._lock:
            if not self._recovery_queue:
                self._recovery_queue.extend(sorted(key for key in self._frames if self._active_key(key)))
            keys = list(self._recovery_queue)
        for key in keys:
            uid = self._uids.get(key)
            if not uid:
                continue
            reconciler.recheck(uid, key[2], now)
            gap = reconciler.recovery_candidate(instrument_uid=uid, interval=key[2], now=now)
            with self._lock:
                if key in self._recovery_queue:
                    self._recovery_queue.remove(key)
            if gap is None or gap >= now:
                continue
            state = reconciler.interval_state(instrument_uid=uid, interval=key[2], open_time=gap, now=now)
            if state == "scheduled_closed":
                continue
            period = pd.Timedelta(seconds=self._tf_period_secs(key[2], gap))
            attempted = True
            try:
                incoming = self._load(self._instruments[key], key[2], start_date=gap,
                                      end_date=gap + period, wait_for_budget=False)
                if incoming is None or incoming.empty:
                    reconciler.record_empty_recovery(instrument_uid=uid, interval=key[2], open_time=gap)
                self._live_merge(key, incoming)
            except RequestDeferred:
                attempted = False
                with self._lock:
                    self._recovery_queue.appendleft(key)
                return
            finally:
                # У фактического RPC свой durable cooldown, даже при пустом ответе.
                if attempted and self._market_store is not None:
                    self._market_store.record_recovery_attempt(source=self._market_source,
                        instrument_uid=uid, interval=key[2], open_time=gap, now=now,
                        next_attempt=now + pd.Timedelta(seconds=self._recovery_retry_seconds))
            return
        if not calendar_turn:
            reconciler.service_calendar(now)

    def prepare_live_history(self, instrument, timeframe, min_bars, *, max_close=None):
        self._live_initial(instrument, timeframe)
        key = self._key(instrument, timeframe)
        for days in (30, 60, 120, 240, 480, 960):
            frame = self.frame_for(instrument, timeframe)
            if max_close is not None and not frame.empty:
                frame = frame[frame["datetime"].apply(lambda stamp: self._timeline.grid(timeframe).bar_close(stamp) <= max_close)]
            if len(frame) >= min_bars:
                return frame
            if days <= self._warmup_days.get(key, 0):
                continue
            with self._lock:
                if key in self._live_busy:
                    return frame
                self._live_busy.add(key)
            try:
                self._live_merge(key, self._load(instrument, timeframe,
                    start_date=_naive(self._timeline.now()) - pd.Timedelta(days=days)))
                self._warmup_days[key] = days
            finally:
                with self._lock:
                    self._live_busy.discard(key)
        frame = self.frame_for(instrument, timeframe)
        if max_close is not None and not frame.empty:
            frame = frame[frame["datetime"].apply(lambda stamp: self._timeline.grid(timeframe).bar_close(stamp) <= max_close)]
        return frame

    def coverage_for(self, instrument, timeframe, frame):
        if frame.empty:
            return False
        if self._market_reconciler is None:
            return True
        uid = self._uids.get(self._key(instrument, timeframe))
        if not uid:
            return False
        period = pd.Timedelta(seconds=self._tf_period_secs(timeframe))
        stamps = list(frame["datetime"])
        for left, right in zip(stamps, stamps[1:]):
            probe = _naive(left) + period
            while probe < _naive(right):
                state = self._market_reconciler.interval_state(instrument_uid=uid,
                    interval=timeframe, open_time=probe, now=self._timeline.now())
                if state not in {"received", "scheduled_closed", "confirmed_no_trade"}:
                    return False
                probe += period
        return True

    def calendar_for(self, instrument):
        uid = self._uids.get(self._key(instrument, "1m")) or getattr(instrument, "uid", None)
        return self._market_reconciler.session_calendar(uid, self._timeline.now()) if uid and self._market_reconciler else None

    def analysis_cursor(self, instrument, timeframe, assignment_id):
        uid = self._uids.get(self._key(instrument, timeframe))
        if not uid or self._market_store is None:
            return None
        return self._market_store.analysis_cursor(source=self._market_source, instrument_uid=uid,
            interval=timeframe, assignment_id=assignment_id)

    def mark_analyzed(self, instrument, timeframe, assignment_id, open_time):
        uid = self._uids.get(self._key(instrument, timeframe))
        if uid and self._market_store is not None:
            self._market_store.mark_analyzed(source=self._market_source, instrument_uid=uid,
                interval=timeframe, assignment_id=assignment_id, open_time=open_time)

    def entry_market(self, instrument):
        """Самодостаточный актуальный снимок для отдельного допуска входа."""
        frame = self.frame_for(instrument, "1m")
        if frame.empty:
            return {"valid": False, "reason": "Нет завершённых базовых данных для текущей цены"}
        stamp = _naive(frame["datetime"].iloc[-1])
        now = _naive(self._timeline.now())
        if now - (stamp + pd.Timedelta(minutes=1)) > pd.Timedelta(seconds=max(60, self._freshness_tolerance_bars * 60 + 60)):
            return {"valid": False, "reason": "Базовая оценка текущей цены устарела"}
        if not self.coverage_for(instrument, "1m", frame.tail(3)):
            return {"valid": False, "reason": "Неполное покрытие базовых данных текущей цены"}
        cal = self.calendar_for(instrument)
        if cal is not None and cal.covers(now) and not cal.is_open(now):
            return {"valid": False, "reason": "Торговая сессия закрыта"}
        current = float(frame["close"].iloc[-1])
        previous = None
        if cal is not None:
            lower = next((a for a, b in cal.intervals if a <= now < b), None)
            if lower is not None:
                prior = frame[frame["datetime"] < lower]
                if not prior.empty:
                    previous = float(prior["close"].iloc[-1])
        uid = self._uids.get(self._key(instrument, "1m"))
        cursors = self._market_store.cursors(source=self._market_source, instrument_uid=uid, interval="1m") if self._market_store and uid else {}
        processed = cursors.get("processed_through")
        return {"valid": True, "current_price": current, "price_time": (stamp + pd.Timedelta(minutes=1)).isoformat(),
            "source": self._market_source, "previous_session_close": previous,
            "session_gap": None if previous is None else current - previous,
            "execution_ready": processed is not None and _naive(pd.Timestamp(processed).tz_localize(None)) >= stamp}

    def _collect_tick(self, timeframe: str, progressed: int, expected: int) -> None:
        """Копит итог текущего рыночного тика по всем таймфреймам.

        ``refresh_if_new_candle`` вызывается по разу на каждый готовый
        таймфрейм, поэтому итог тика нельзя фиксировать на каждом вызове: два
        старших таймфрейма, у которых на этом тике и не должно было быть нового
        бара, объявили бы конец данных уже на первом тике. Решение принимает
        ``close_tick`` один раз на тик, когда известен итог целиком.

        Таймфрейм учитывается в ``expected`` один раз за тик: повторный вызов с
        ``force`` для того же ТФ не должен удваивать ожидаемое число баров.
        """
        self._tick_progressed += progressed
        if expected:
            self._tick_expected.add(timeframe)

    @_synchronized
    def close_tick(self) -> None:
        """Закрывает учёт рыночного тика: одна запись в счётчики прогона за тик.

        Тик, в котором ни один таймфрейм не ждал нового бара, ничего не говорит о
        доступности данных и в счётчики не попадает.
        """
        if not self._history:
            return
        progressed, expected = self._tick_progressed, len(self._tick_expected)
        self._tick_progressed = 0
        self._tick_expected.clear()
        if not expected:
            return
        self._register_tick(progressed, expected)

    @_synchronized
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
                if key[2] == timeframe and self._active_key(key)
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
            if key[2] != timeframe or not self._active_key(key):
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
        На виртуальных часах пауза между загрузками не нужна: данных прошлого
        периода не требует дожидаться публикации.
        """
        if self._history:
            return
        if not self._data_refresh_min_interval or self._last_api_attempt is None:
            return
        elapsed = (_naive(self._timeline.now()) - self._last_api_attempt).total_seconds()
        remaining = self._data_refresh_min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _note_new_bar(self, key: tuple, closed: pd.DataFrame) -> bool:
        """Отмечает появление нового закрытого бара пары. ``True`` — бар новый."""
        last = _naive(closed["datetime"].max()) if not closed.empty else None
        previous = self._seen.get(key)
        self._seen[key] = last
        return last is not None and (previous is None or last > previous)

    def _register_tick(self, progressed: int, expected: int) -> None:
        """Учёт тика исторического прогона: прогресс, разрывы и конец данных.

        Тик без прогресса не обрывает прогон сам по себе: копящийся счётчик
        превращается в разрыв данных, и кэш ищет следующий имеющийся бар. Бар
        найден — разрыв уходит прогону (рыночное время переводит сессия
        прогона), бар не найден — сигнализируется конец доступных данных. Если
        источник не ответил, конец данных не фиксируется и поиск повторяется.
        """
        if progressed:
            self._empty_ticks = 0
            if self._gap is not None:
                self._close_gap(self._last_closed_bar())
            else:
                self._missed_bars += max(0, expected - progressed)
            return
        self._empty_ticks += 1
        if self._gap is None:
            now = _naive(self._timeline.now())
            self._gap = DataGap(
                since=now,
                resume=now,
                missed=0,
                last_bar=self._last_closed_bar(),
                empty_ticks=self._empty_ticks,
            )
        self._gap.empty_ticks = self._empty_ticks
        if self._empty_ticks < CONSECUTIVE_EMPTY_TICKS_BEFORE_END:
            return
        self._resolve_gap()

    def _resolve_gap(self) -> None:
        """Разбирает накопленный счётчик тиков без прогресса: разрыв или конец данных."""
        gap = self._gap
        if gap is None:  # pragma: no cover - защита от вызова без открытого разрыва
            return
        now = _naive(self._timeline.now())
        resume, source_failed = self._find_next_bar(now)
        if source_failed:
            # Источник не ответил: конец данных не фиксируется, потому что
            # отсутствие ответа ничего не говорит о наличии баров. Разрыв остаётся
            # открытым, и поиск повторяется на следующих тиках.
            return
        event = self._next_contract_event(now) if self._next_contract_event is not None else None
        if event is not None and now < _naive(event) <= self._gap_search_ceiling(now) and (resume is None or _naive(event) <= resume):
            self._pending_calendar = event
            self._empty_ticks = 0
            return
        if resume is None and self._confirm_terminal_gap(now):
            return
        self._gap = None
        if resume is None:
            horizon_end = self._gap_search_ceiling(now)
            range_end = self._history_range_end()
            limited = range_end is None or horizon_end < range_end
            self.data_exhausted = True
            self.data_exhaustion = DataExhaustion(
                at=now,
                horizon_start=now,
                horizon_end=horizon_end,
                range_end=None if range_end is None else _naive(range_end),
                horizon_limited=limited,
            )
            if limited:
                log.info(
                    "Следующего бара нет в проверенном горизонте %s — %s: поиск ограничен семью сутками, "
                    "отсутствие баров после него не утверждается — конец доступных данных.",
                    now, horizon_end,
                )
            else:
                log.info(
                    "Новые бары не появились %d тиков подряд, следующего бара до %s нет — конец доступных данных.",
                    gap.empty_ticks,
                    horizon_end,
                )
            return
        gap.resume = resume
        gap.missed = self._bars_between(gap.last_bar, resume)
        gap.span = self._gap_span(gap, resume)
        self._missed_bars += gap.missed
        self._gaps += 1
        self._longest_gap = max(self._longest_gap, gap.span)
        self._pending_gap = gap
        self._empty_ticks = 0
        log.info(
            "Разрыв данных с %s: пропущено баров %d, следующий имеющийся бар %s.",
            gap.since, gap.missed, resume,
        )

    def _close_gap(self, resume) -> None:
        """Данные возобновились без перевода часов: разрыв закрыт, пропуски посчитаны."""
        gap, self._gap = self._gap, None
        if gap is None or resume is None:
            return
        gap.resume = _naive(resume)
        gap.missed = self._bars_between(gap.last_bar, gap.resume)
        gap.span = self._gap_span(gap, gap.resume)
        self._missed_bars += gap.missed
        self._gaps += 1
        self._longest_gap = max(self._longest_gap, gap.span)
        log.info(
            "Разрыв данных с %s закрыт: пропущено баров %d, данные возобновились с %s.",
            gap.since, gap.missed, gap.resume,
        )

    def _gap_span(self, gap: DataGap, resume) -> pd.Timedelta:
        """Длительность промежутка, на котором баров не было.

        Считается от бара, следующего за последним имеющимся, до следующего
        имеющегося: так длительность совпадает с подсчётом разрывов в
        предварительной проверке (пропущенные бары, а не расстояние между
        соседними барами).
        """
        timeframe = self._driver_timeframe()
        if gap.last_bar is not None and timeframe is not None:
            period = pd.Timedelta(seconds=self._tf_period_secs(timeframe))
            return max(pd.Timedelta(0), _naive(resume) - (_naive(gap.last_bar) + period))
        return max(pd.Timedelta(0), _naive(resume) - _naive(gap.since))

    def _find_next_bar(self, now) -> tuple[pd.Timestamp | None, bool]:
        """Ищет ближайший имеющийся закрытый бар мельчайшего активного ТФ после ``now``.

        Один запрос на каждый кадр мельчайшего таймфрейма в окне
        ``[now, min(now + 7 суток, конец диапазона)]``. Границы окна источник
        трактует включительно, поэтому кандидаты отбираются строго вручную: бар на
        текущем рыночном моменте ещё не закрыт, а бар за верхней границей поиска
        уже за горизонтом. Кадры старших таймфреймов не опрашиваются: их бары
        внутри разрыва тоже отсутствуют, и ответ они не изменят.

        Возвращает пару ``(момент следующего бара, не ответил ли источник)``:
        молчание источника не должно выдаваться за конец доступных данных.
        """
        timeframe = self._driver_timeframe()
        if timeframe is None:
            return None, False
        ceiling = self._gap_search_ceiling(now)
        if ceiling <= now:
            return None, False
        best: pd.Timestamp | None = None
        failed = False
        for key, frame in list(self._frames.items()):
            if key[2] != timeframe or not self._active_key(key):
                continue
            try:
                probe = self._load(
                    self._instruments[key],
                    timeframe,
                    start_date=now,
                    end_date=ceiling,
                )
            except Exception as exc:
                log.warning("Не удалось найти следующий бар для %s: %s", key, exc)
                self._failed_reads.add(key)
                failed = True
                continue
            if probe is None or probe.empty or "datetime" not in probe.columns:
                continue
            stamps = _naive_series(probe["datetime"])
            later = stamps[(stamps > now) & (stamps < ceiling)]
            if later.empty:
                continue
            candidate = later.min()
            if best is None or candidate < best:
                best = candidate
        if best is None and failed:
            return None, True
        return best, False

    def _history_range_end(self):
        """Правая граница диапазона прогона (у истории она есть всегда)."""
        return getattr(self._clock, "end", None) or getattr(self._timeline, "end", None)

    def _gap_search_ceiling(self, now) -> pd.Timestamp:
        """Верхняя граница поиска следующего бара: 7 суток, но не за концом диапазона."""
        ceiling = _naive(now) + GAP_SEARCH_LOOKAHEAD
        end = self._history_range_end()
        if end is not None:
            ceiling = min(ceiling, _naive(end))
        return ceiling

    def _driver_timeframe(self) -> str | None:
        """Мельчайший активный таймфрейм: по нему считаются тик прогона и пропуски."""
        timeframes = {key[2] for key in self._frames if self._active_key(key)}
        if not timeframes:
            return None
        now = self._timeline.now()
        return min(timeframes, key=lambda tf: self._tf_period_secs(tf, now))

    def _last_closed_bar(self, timeframe: str | None = None) -> pd.Timestamp | None:
        """Момент последнего закрытого бара мельчайшего активного ТФ по всем его кадрам."""
        target = timeframe or self._driver_timeframe()
        if target is None:
            return None
        last: pd.Timestamp | None = None
        for key, frame in self._frames.items():
            if key[2] != target or not self._active_key(key) or frame is None or frame.empty:
                continue
            closed = self._closed_only(frame, target)
            if closed.empty:
                continue
            candidate = _naive(closed["datetime"].max())
            if last is None or candidate > last:
                last = candidate
        return last

    def _bars_between(self, start, end) -> int:
        """Сколько баров мельчайшего активного ТФ пропало между двумя имеющимися барами."""
        if start is None or end is None:
            return 0
        timeframe = self._driver_timeframe()
        if timeframe is None:
            return 0
        period = self._tf_period_secs(timeframe)
        if period <= 0:
            return 0
        slots = (_naive(end) - _naive(start)).total_seconds() / period
        return max(0, int(slots) - 1)

    def _closed_only(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        grid = self._timeline.grid(timeframe)
        boundary = _naive(grid.current_candle_start(self._timeline.now()))
        complete = frame["is_complete"].astype(bool) if "is_complete" in frame.columns else True
        return frame[(frame["datetime"] < boundary) & complete].copy()

    def _resolved_only(self, key: tuple, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Не выдаёт стратегиям свечи после первой неразрешённой минуты.

        В историческом режиме прежняя семантика остаётся неизменной. В живом
        режиме состояние приходит из durable store и календаря брокера.
        """
        if self._market_reconciler is None or frame.empty or timeframe != "1m":
            return frame
        instrument_id = self._uids.get(key)
        if not instrument_id:
            return frame.iloc[0:0].copy()
        ordered = frame.sort_values("datetime")
        period = pd.Timedelta(minutes=1)
        allowed: list[int] = []
        previous = None
        for index, row in ordered.iterrows():
            stamp = _naive(row["datetime"])
            if previous is not None:
                probe = previous + period
                while probe < stamp:
                    state = self._market_reconciler.interval_state(
                        instrument_uid=instrument_id, interval=timeframe,
                        open_time=probe, now=self._timeline.now(),
                    )
                    if state not in {"received", "scheduled_closed", "confirmed_no_trade"}:
                        self._report_coverage_block(key, probe, state)
                        return ordered.loc[allowed].copy()
                    probe += period
            allowed.append(index)
            previous = stamp
        return ordered.loc[allowed].copy()

    def _report_coverage_block(self, key: tuple, stamp: pd.Timestamp, state: str) -> None:
        marker = (*key, _naive(stamp), state)
        if marker in self._coverage_blocks:
            return
        self._coverage_blocks.add(marker)
        log.warning(
            "Симуляция %s остановлена перед неразрешённой свечой %s (%s)",
            key, _naive(stamp), state,
        )

    def _recover_oldest_gap(self, key: tuple, timeframe: str, now) -> tuple[pd.DataFrame | None, bool]:
        """Один bounded запрос старейшей дыры, отдельно от свежего окна."""
        if self._market_reconciler is None or self._history:
            return None, False
        instrument_id = self._uids.get(key)
        if not instrument_id:
            return None, False
        gap = self._market_reconciler.recovery_candidate(instrument_uid=instrument_id, interval=timeframe)
        if gap is None or gap >= _naive(now):
            return None, False
        period = pd.Timedelta(seconds=self._tf_period_secs(timeframe, gap))
        try:
            recovered = self._load(
                self._instruments[key], timeframe, start_date=gap, end_date=gap + period,
            )
        except Exception as exc:
            if "resource_exhausted" in str(exc).lower():
                self._retry_after = self._pause_after_rate_limit(exc, _naive(now))
                return None, True
            raise
        if recovered is None or recovered.empty:
            self._market_reconciler.record_empty_recovery(
                instrument_uid=instrument_id, interval=timeframe, open_time=gap,
            )
        return recovered, True

    def _merge_new_bars(self, frame, new_df, last_dt):
        if new_df is None or new_df.empty:
            return frame
        # Нельзя отбрасывать строку только потому, что её время старше
        # последней загруженной свечи: брокер способен опубликовать 12:01
        # после уже полученной 12:03 либо исправить старую свечу. Последняя
        # версия того же open time заменяет предыдущую.
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
