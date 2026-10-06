from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from src.api.retry import DEFAULT_BASE_DELAY, rate_limit_reset_secs
from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)

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

    def __init__(self, loader, timeline, token=None, data_refresh_min_interval=0.0, data_backfill_window_seconds=None, freshness_tolerance_bars=0, clock=None, client_provider=None) -> None:
        self._loader = loader
        self._timeline = timeline  # MultiTimeframeScheduler: сетки и рыночное время
        self._token = token
        self._clock = clock
        self._history = bool(getattr(clock, "is_virtual", False))
        self._client_provider = client_provider
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
        return (instrument.ticker, instrument.instrument_type, timeframe)

    def _source_kwargs(self) -> dict:
        """Аргументы источника данных для загрузчика.

        Боевой режим ничего не добавляет: загрузчик сам берёт системное время и
        провайдер по умолчанию. Исторический прогон передаёт свой источник и
        виртуальные часы, иначе границы дозагрузки считались бы от «сейчас».
        """
        if not self._history:
            return {}
        return {"client_provider": self._client_provider, "clock": self._clock}

    def _load(self, instrument, timeframe: str, start_date=None, end_date=None) -> pd.DataFrame:
        """Дозагрузка кадра по окну ``[start_date, end_date)``.

        Обычная дозагрузка ``end_date`` не задаёт: в историческом прогоне загрузчик
        сам ограничивает срез текущим рыночным моментом. Явное ``end_date`` нужно
        только поиску следующего бара за разрывом — там окно ограничено горизонтом
        поиска, а не рыночным временем.
        """
        key = self._key(instrument, timeframe)
        self._last_api_attempt = _naive(self._timeline.now())
        df, instrument_id = self._loader(
            ticker=instrument.ticker,
            instrument_type=instrument.instrument_type,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            token=self._token,
            instrument_id=self._uids.get(key),
            **self._source_kwargs(),
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
        progressed = 0
        for _, key in candidates:
            if not self._history and self._throttled(now):
                return
            frame = self._frames[key]
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
            self._last_loaded[key] = _naive(closed["datetime"].max()) if not closed.empty else last_dt
            self._observed[key] = boundary
            if self._note_new_bar(key, closed):
                progressed += 1
        if self._history:
            self._collect_tick(timeframe, progressed, len(candidates))

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
            if key[2] != timeframe or frame is None or frame.empty:
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
                failed = True
                continue
            if probe is None or probe.empty or "datetime" not in probe.columns:
                continue
            stamps = _naive_series(probe["datetime"])
            later = stamps[(stamps > now) & (stamps <= ceiling)]
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
        timeframes = {key[2] for key in self._frames}
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
            if key[2] != target or frame is None or frame.empty:
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
