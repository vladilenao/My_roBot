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
from src.data.coverage_states import RESOLVED_INTERVAL_STATES
from src.data.live_state import normalize_rpc_candle, normalize_stream_candle

log = get_logger(__name__)

# Вид разрыва графика по тяжести и вид разрешённых состояний покрытия
# (telegram-trade-cards, data-cache «Классификация разрывов окна»).
_CHART_BREAK_ORDER = ("closed", "no_trade", "schedule_unavailable", "missing")
_CHART_RESOLVED_KINDS = {"scheduled_closed": "closed", "confirmed_no_trade": "no_trade", "source_absent": "no_trade"}


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

    def __init__(self, loader, timeline, token=None, data_refresh_min_interval=0.0, data_backfill_window_seconds=None, freshness_tolerance_bars=0, clock=None, client_provider=None, market_store=None, market_source="tbank_exchange", market_reconciler=None, market_calendar=None, request_budget=None, recovery_retry_seconds=65, live_cutover=None) -> None:
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
        self._market_calendar = market_calendar
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
        # Live-режим всегда владеет входом через единый accept-путь.
        self._owned_live_ingestion = self.live_session_enabled
        self._live_conflicts = set()
        self._pending_execution_lower = {}
        # Решение владельца (вариант «б», проба 8.5): прошлые даты брокер не
        # отдаёт, поэтому без сохранённых обязательств непрерывность
        # доказывается от момента запуска; строгой остаётся 1m-граница
        # исполнения сохранённых обязательств.
        self._live_cutover = (None if not self.live_session_enabled
                              else _naive(live_cutover if live_cutover is not None else timeline.now()))
        self._pre_cutover_gaps = {}
        self._live_members = {}
        self._live_evidence = {}
        # Свечи минут, которые исполнение могло пройти как «у источника нет»:
        # главный поток пишет по ним отметку в журнал сделки.
        self._late_after_source_absent = deque()
        self._live_generation = 0
        self._data_changed = threading.Condition(self._lock)
        self._pending_live_history = {}
        # Все потребности этого запуска (адрес, тикер, тип, ТФ): по ним пары
        # сохранённого состава, которые никто не запросил, снимаются.
        self._requested_live = set()

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

    # ── Live: единственный владелец рыночного входа (design 2, 6, 9) ──
    # Торговый поток только читает неизменяемые копии кадров и регистрирует
    # потребности. Сеть, прогрев, свежая дозагрузка и recovery выполняет
    # фоновая полоса ingestion (src/data/ingestion.py) через accept-путь ниже.

    def register_live_pair(self, instrument, uid, timeframe, min_bars=1):
        """UID/alias mapping пары; не читает финансовую БД или сеть."""
        if self._history or self._market_store is None:
            raise ValueError("Owned live-ingestion требует отдельного рыночного хранилища")
        if not uid or isinstance(min_bars, bool) or not isinstance(min_bars, int) or min_bars <= 0:
            raise ValueError("Live-пара требует UID и положительный прогрев")
        with self._lock:
            key = self._key(instrument, timeframe)
            previous = self._uids.get(key)
            owners = {other for (other, tf), member in self._live_members.items()
                      if tf == timeframe and key in member["keys"]}
            if (previous and previous != uid) or owners - {uid}:
                raise ValueError("Неоднозначный финансовый alias разных UID")
            self._owned_live_ingestion = True
            self._uids[key] = uid
            self._instruments[key] = instrument
            member = self._live_members.setdefault((uid, timeframe), {
                "keys": set(), "min_bars": 0, "execution_lower": None, "instrument": instrument,
                "history": "pending", "warmup_days": 0, "history_error": "", "state": "BOOTSTRAPPING",
            })
            member["keys"].add(key)
            # Пара сохранённого состава, затем выбранная пользователем: дальше
            # обслуживается выбранным инструментом (семейство, полное имя).
            member["instrument"] = instrument
            lower = self._pending_execution_lower.pop(instrument_key(instrument), None)
            if lower is not None and timeframe == "1m":
                previous = member["execution_lower"]
                member["execution_lower"] = lower if previous is None else min(previous, lower)
            if min_bars > member["min_bars"]:
                member["min_bars"] = min_bars
                if member["history"] == "done" and len(self._closed_member_frame(member, timeframe)) < min_bars:
                    member["history"] = "pending"
            if key not in self._frames:
                # Market-only disk read, никакой implicit первичной RPC загрузки.
                self._frames[key] = self._market_store.current_frame(
                    source=self._market_source, instrument_uid=uid, interval=timeframe)
                if not self._frames[key].empty:
                    closed = self._closed_only(self._frames[key], timeframe)
                    if not closed.empty:
                        self._last_loaded[key] = _naive(closed["datetime"].max())
            self._advance_live_coverage(uid, timeframe, member)
            self._refresh_member_state(uid, timeframe, member)
            self._live_generation += 1
            self._data_changed.notify_all()

    def unregister_live_pair(self, uid, timeframe):
        """Снимает пару, не выбранную в этом запуске и без обязательств.

        Данные в рыночной базе остаются; снимается только обслуживание:
        свежий опрос, подписка и полосы её больше не видят.
        """
        with self._lock:
            member = self._live_members.pop((uid, timeframe), None)
            if member is None:
                return False
            for key in member["keys"]:
                self._uids.pop(key, None)
                self._instruments.pop(key, None)
                self._frames.pop(key, None)
                self._last_loaded.pop(key, None)
            self._live_evidence.pop((uid, timeframe), None)
            self._live_generation += 1
            self._data_changed.notify_all()
            return True

    def retire_unrequested_live_pairs(self):
        """Снимает пары, ТФ которых этот запуск не запросил (retire-unrequired-roster-pairs).

        Пара сохранённого состава регистрируется до выбора без потребности
        прогрева. Если после регистрации потребностей стратегий, фильтров и
        сопровождения сделок её ТФ не нужен никому, прогрев не начнётся
        никогда, и старт ждал бы её вечно. Запрос узнаётся по UID или адресу
        пары либо по тикеру и типу: live-выбор строит инструменты без UID.
        Возвращает снятые пары как (UID, ТФ, тикер).
        """
        with self._lock:
            stale = []
            for (uid, timeframe), member in sorted(self._live_members.items()):
                instrument = member["instrument"]
                addresses = {uid} | {key[0] for key in member["keys"]}
                if not any(tf == timeframe and (address in addresses or (
                        ticker == instrument.ticker and kind == instrument.instrument_type))
                        for address, ticker, kind, tf in self._requested_live):
                    stale.append((uid, timeframe, instrument.ticker))
            for uid, timeframe, _ in stale:
                self.unregister_live_pair(uid, timeframe)
        return stale

    def set_live_execution_lower(self, uid, timeframe, open_time):
        """Main-thread boundary сохранённых обязательств, не high-water receipt."""
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is None:
                raise ValueError("Execution boundary требует зарегистрированную live-пару")
            stamp = _naive(open_time)
            previous = member["execution_lower"]
            member["execution_lower"] = stamp if previous is None else min(previous, stamp)
            self._advance_live_coverage(uid, timeframe, member)

    def note_execution_lower(self, instrument, open_time):
        """Граница сохранённых обязательств до регистрации пары (main thread)."""
        with self._lock:
            uid = self._uids.get(self._key(instrument, "1m"))
            if not uid or (uid, "1m") not in self._live_members:
                self._pending_execution_lower[instrument_key(instrument)] = _naive(open_time)
                return
        self.set_live_execution_lower(uid, "1m", open_time)

    def set_stream_evidence(self, uid, timeframe, evidence):
        """ACK текущего поколения: доказательство режима закрытия stream-свечей."""
        with self._lock:
            if evidence is None:
                self._live_evidence.pop((uid, timeframe), None)
            else:
                self._live_evidence[uid, timeframe] = evidence
            member = self._live_members.get((uid, timeframe))
            if member is not None:
                self._refresh_member_state(uid, timeframe, member)

    def set_live_handoff(self, uid, timeframe, *, transport, epoch=None, ready):
        """Supervisor закрывает/открывает handoff пары после ACK и overlap."""
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is None:
                return
            member["transport"] = transport
            member["epoch"] = epoch
            member["handoff_ready"] = bool(ready)
            self._refresh_member_state(uid, timeframe, member)
            self._live_generation += 1
            self._data_changed.notify_all()

    def accept_live_observation(self, observation, *, reconcile_sequence=None):
        """Единственный accept/normalize/reconcile/publish путь live-транспортов."""
        return self._accept_batch([observation], [reconcile_sequence])[0]

    def _accept_batch(self, observations, sequences):
        """Одна транзакция store, одно слияние кадра и одно продвижение префикса."""
        with self._lock:
            members = {}
            for item in observations:
                member = self._live_members.get((item.uid, item.timeframe))
                if member is None:
                    raise ValueError("Наблюдение не принадлежит зарегистрированному UID/ТФ")
                members[item.uid, item.timeframe] = member
            if not observations:
                return []
            results = self._market_store.accept_observations(observations, source=self._market_source,
                                                             reconcile_sequences=sequences)
            changed, notify = {}, False
            for item, sequence, result in zip(observations, sequences, results):
                pair, stamp = (item.uid, item.timeframe), _naive(item.open_time)
                if result["state"] == "accepted" and result["changed"]:
                    changed.setdefault(pair, []).append(item)
                    if result.get("previous_coverage") == "source_absent" and item.timeframe == "1m":
                        row = item.as_row()
                        self._late_after_source_absent.append((item.uid, stamp, tuple(
                            float(row[name]) for name in ("open", "high", "low", "close"))))
                    if result.get("previous_coverage") == "scheduled_closed":
                        self._market_store.save_live_state(
                            {"outside_schedule": True, "open_time": stamp.isoformat()},
                            kind="calendar", source=self._market_source,
                            instrument_uid=item.uid, interval=item.timeframe, open_time=stamp,
                        )
                if result["state"] == "conflict":
                    self._live_conflicts.add((*pair, stamp))
                elif result["state"] == "accepted" and sequence is not None:
                    self._live_conflicts.discard((*pair, stamp))
                if result["changed"] or result["state"] == "conflict" or (sequence is not None and result["state"] == "accepted"):
                    notify = True
            for pair, items in changed.items():
                member = members[pair]
                incoming = pd.DataFrame([item.as_row() for item in items])
                closed_opens = [_naive(item.open_time) for item in items if item.closed]
                for key in member["keys"]:
                    frame = self._frames.get(key)
                    self._frames[key] = (incoming.sort_values("datetime").drop_duplicates("datetime", keep="last").reset_index(drop=True)
                                         if frame is None or frame.empty else self._merge_new_bars(frame, incoming, self._last_loaded.get(key)))
                    if closed_opens:
                        self._last_loaded[key] = max(max(closed_opens), self._last_loaded.get(key, pd.Timestamp.min))
                self._advance_live_coverage(*pair, member)
            for pair, member in members.items():
                self._refresh_member_state(*pair, member)
            if notify:
                self._live_generation += 1
                self._data_changed.notify_all()
            return results

    def live_floor(self, uid, timeframe):
        """Нижняя граница проверки непрерывности пары или None (строго)."""
        member = self._live_members.get((uid, timeframe))
        if self._live_cutover is None or (member is not None and member["execution_lower"] is not None):
            return None
        return _naive(self._timeline.grid(timeframe).previous_candle_start(self._live_cutover))

    def _advance_live_coverage(self, uid, timeframe, member):
        """Непрерывный префикс от meaningful lower; ночи и перерывы — по календарю.

        Нижняя граница выбирается по прогреву и durable execution, не по
        latest receipt. Сканирование инкрементально: от уже разрешённой
        границы, с прыжком через подтверждённые календарём перерывы.
        """
        frames = [self._frames.get(key) for key in member["keys"]]
        frame = next((item for item in frames if item is not None and not item.empty), None)
        if frame is None:
            return None
        closed = self._closed_only(frame, timeframe).sort_values("datetime")
        lowers = []
        if len(closed) >= member["min_bars"] and not closed.empty:
            lowers.append(_naive(closed["datetime"].iloc[-member["min_bars"]]))
        if member["execution_lower"] is not None:
            lowers.append(member["execution_lower"])
        if not lowers:
            return None
        cursor = self._market_store.cursors(source=self._market_source, instrument_uid=uid, interval=timeframe)
        resolved = cursor.get("resolved_through")
        resolved = None if resolved is None else pd.Timestamp(resolved).tz_convert("UTC").tz_localize(None)
        lower = min(lowers)
        floor = self.live_floor(uid, timeframe)
        if floor is not None:
            lower = max(lower, floor)
        if member.get("coverage_lower") is None:
            stored = self._market_store.live_state(kind="pair", source=self._market_source,
                                                   instrument_uid=uid, interval=timeframe).get("coverage_lower")
            member["coverage_lower"] = None if stored is None else _naive(stored)
        existing = member.get("coverage_lower")
        if existing is not None and (floor is None or existing >= floor) and (resolved is None or resolved < lower):
            # Граница не поднимается через недоказанный участок: новый
            # high-water не прячет внутреннюю/хвостовую дыру (задача 2.5).
            lower = min(existing, lower)
        if member.get("coverage_lower") != lower:
            member["coverage_lower"] = lower
            self._market_store.save_live_state({"coverage_lower": lower.isoformat()}, kind="pair",
                source=self._market_source, instrument_uid=uid, interval=timeframe)
        grid = self._timeline.grid(timeframe)
        opens = _naive_series(closed["datetime"])
        if resolved is not None and resolved >= lower:
            previous, probe = resolved, _naive(grid.bar_close(resolved))
        elif bool((opens == lower).any()):
            previous, probe = lower, _naive(grid.bar_close(lower))
        else:
            previous, probe = None, lower
        # Только свечи после уже доказанной границы: без O(n) на каждую свечу.
        stamps = [_naive(stamp) for stamp in opens[opens >= probe]]
        now = self._timeline.now()
        for stamp in stamps:
            if stamp < probe:
                continue
            while probe < stamp:
                jump = self._resolved_jump(uid, timeframe, probe, now)
                if jump is None:
                    break
                probe = jump
            if probe < stamp:
                break
            previous, probe = stamp, _naive(grid.bar_close(stamp))
        if previous is not None and (resolved is None or previous > resolved):
            self._market_store.mark_resolved(source=self._market_source, instrument_uid=uid,
                                             interval=timeframe, open_time=previous)
        return previous

    def _resolved_jump(self, uid, timeframe, probe, now):
        """Следующая проверяемая точка после разрешённого интервала либо None."""
        grid = self._timeline.grid(timeframe)
        reconciler = self._market_reconciler
        if reconciler is None:
            state = self._market_store.coverage_state(source=self._market_source, instrument_uid=uid,
                                                      interval=timeframe, open_time=probe)
        else:
            state = reconciler.interval_state(instrument_uid=uid, interval=timeframe, open_time=probe, now=now)
        if state not in RESOLVED_INTERVAL_STATES:
            return None
        following = _naive(grid.bar_close(probe))
        if state == "scheduled_closed" and reconciler is not None:
            calendar = reconciler.session_calendar(uid, now)
            if calendar is not None and calendar.covers(probe):
                opening = calendar.next_open(probe)
                if opening is not None and calendar.covers(opening):
                    following = max(following, _naive(grid.current_candle_start(opening)))
        return following

    def _closed_member_frame(self, member, timeframe):
        frames = [self._frames.get(key) for key in member["keys"]]
        frame = next((item for item in frames if item is not None and not item.empty), None)
        return pd.DataFrame() if frame is None else self._closed_only(frame, timeframe)

    def _refresh_member_state(self, uid, timeframe, member):
        """Data-state отдельно от транспорта (design 9)."""
        held = self._market_store.live_state(kind="uid", source=self._market_source, instrument_uid=uid)
        if held.get("integrity_hold"):
            state = "INTEGRITY_HOLD"
        elif any(item[:2] == (uid, timeframe) for item in self._live_conflicts):
            state = "CONFLICT_HOLD"
        elif member["history"] != "done":
            state = "BOOTSTRAPPING"
        elif member.get("transport") == "stream" and not member.get("handoff_ready"):
            state = "RECONCILING"
        elif member.get("gap"):
            state = "GAP_HOLD"
        else:
            state = "READY"
        member["state"] = state
        return state

    def accept_stream_candle(self, candle, *, epoch, received_at):
        evidence = self._live_evidence.get((candle.uid, candle.timeframe))
        observation = normalize_stream_candle(candle, evidence=evidence, epoch=epoch, received_at=received_at)
        return self.accept_live_observation(observation)

    def accept_rpc_frame(self, uid, timeframe, frame, *, received_at, fences=None):
        """Worker передаёт страницу после RPC; network не входит в owner lock."""
        sources = frame.attrs.get("actual_candle_sources", {})
        observations = [normalize_rpc_candle(row, uid=uid, timeframe=timeframe, received_at=received_at,
                            actual_source=sources.get(pd.Timestamp(row["datetime"]).isoformat()))
                        for row in frame.to_dict("records")]
        sequences = [None if fences is None else fences.get(pd.Timestamp(item.open_time).isoformat())
                     for item in observations]
        return self._accept_batch(observations, sequences)

    def reconciliation_fences(self, uid, timeframe, frame):
        """Версии наблюдений ДО начала согласующего RPC, не после его ответа."""
        return {pd.Timestamp(stamp).isoformat(): self._market_store.live_state(
            kind="candle", source=self._market_source, instrument_uid=uid, interval=timeframe,
            open_time=stamp).get("observed_sequence", 0) for stamp in frame["datetime"]}

    def live_conflicts(self):
        with self._lock:
            return sorted(self._live_conflicts)

    def pin_execution_input(self, instrument, stamp):
        with self._lock:
            uid = self._uids.get(self._key(instrument, "1m"))
            if not uid:
                raise ValueError("Нет UID для закрепления исполнительного входа")
            reference = self._market_store.pin_input(source=self._market_source, instrument_uid=uid,
                                                    interval="1m", open_time=stamp)
            return reference, self._market_store.pinned_frame(reference)

    def resume_execution_marker(self, instrument, financial_boundary):
        """После сбоя между financial commit и market-marker: без replay минуты.

        Финансовая граница уже подтверждена; отмечается обработанной только
        ранее закреплённая версия начатого пакета. Исправленная current-версия
        не проигрывается, а конфликт с ней превращается в integrity-hold.
        """
        with self._lock:
            uid = self._uids.get(self._key(instrument, "1m"))
            if not uid or financial_boundary is None:
                return None
            stamp = _naive(financial_boundary)
            processed = self._market_store.cursors(source=self._market_source, instrument_uid=uid,
                                                   interval="1m").get("processed_through")
            if processed is not None and pd.Timestamp(processed).tz_convert("UTC").tz_localize(None) >= stamp:
                return None
            self._market_store.mark_processed(source=self._market_source, instrument_uid=uid,
                                              interval="1m", open_time=stamp)
            return stamp

    def wait_for_data_change(self, generation, timeout):
        with self._data_changed:
            self._data_changed.wait_for(lambda: self._live_generation != generation, timeout=timeout)
            return self._live_generation

    @property
    def data_generation(self):
        with self._lock:
            return self._live_generation

    def request_live_history(self, instrument, timeframe, min_bars):
        """Недостающая потребность передаётся worker без синхронной сети."""
        with self._lock:
            key = self._key(instrument, timeframe)
            self._requested_live.add((key[0], instrument.ticker, instrument.instrument_type, timeframe))
            self._instruments.setdefault(key, instrument)
            uid = self._uids.get(key)
            member = self._live_members.get((uid, timeframe)) if uid else None
            if member is not None and member["history"] == "done" and member["min_bars"] >= min_bars:
                return self._closed_member_frame(member, timeframe)
            previous = self._pending_live_history.get(key, (None, 0))[1]
            self._pending_live_history[key] = (instrument, max(min_bars, previous))
            frame = self._frames.get(key)
            return pd.DataFrame() if frame is None or frame.empty else self._closed_only(frame, timeframe)

    def live_needs(self):
        """Снимок потребностей прогрева/регистрации для фонового worker."""
        with self._lock:
            return [(key, instrument, min_bars) for key, (instrument, min_bars) in self._pending_live_history.items()]

    def finish_live_need(self, key, min_bars):
        with self._lock:
            current = self._pending_live_history.get(key)
            if current is not None and current[1] <= min_bars:
                self._pending_live_history.pop(key, None)

    def live_pairs(self):
        """Снимок зарегистрированных пар: worker не держит owner lock во время RPC."""
        with self._lock:
            return [self._pair_snapshot(uid, tf, member) for (uid, tf), member in sorted(self._live_members.items())]

    def live_pair(self, uid, timeframe):
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            return None if member is None else self._pair_snapshot(uid, timeframe, member)

    def _pair_snapshot(self, uid, timeframe, member):
        key = min(member["keys"])
        frame = next((self._frames.get(item) for item in member["keys"]
                      if self._frames.get(item) is not None and not self._frames[item].empty), None)
        return dict(uid=uid, timeframe=timeframe, instrument=member["instrument"], min_bars=member["min_bars"],
                    history=member["history"], warmup_days=member["warmup_days"], state=member["state"],
                    transport=member.get("transport"), key=key,
                    closed=0 if frame is None else self._closed_count(frame, timeframe),
                    last_closed=self._last_loaded.get(key))

    def _closed_count(self, frame, timeframe):
        boundary = _naive(self._timeline.grid(timeframe).current_candle_start(self._timeline.now()))
        mask = frame["datetime"] < boundary
        if "is_complete" in frame.columns:
            mask &= frame["is_complete"].astype(bool)
        return int(mask.sum())

    def mark_live_history(self, uid, timeframe, *, days=None, done=None, error=None):
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is None:
                return
            if days is not None:
                member["warmup_days"] = max(member["warmup_days"], days)
            if error is not None:
                member["history_error"] = error
            if done is not None:
                member["history"] = "done" if done else "pending"
                if done and error is None:
                    member["history_error"] = ""
            self._advance_live_coverage(uid, timeframe, member)
            self._refresh_member_state(uid, timeframe, member)
            self._live_generation += 1
            self._data_changed.notify_all()

    def refresh_live_coverage(self, uid, timeframe):
        """Пересчёт префикса после нового доказательства без свечи (no-trade)."""
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is None:
                return
            self._advance_live_coverage(uid, timeframe, member)
            self._refresh_member_state(uid, timeframe, member)
            self._live_generation += 1
            self._data_changed.notify_all()

    def set_live_gap(self, uid, timeframe, gap):
        """Recovery-владелец сообщает об активном/парковочном диапазоне пары."""
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is None:
                return
            member["gap"] = gap
            self._refresh_member_state(uid, timeframe, member)

    def set_lane_status(self, uid, timeframe, lane, status) -> None:
        """Сводка фоновой полосы по паре для машиночитаемой диагностики."""
        with self._lock:
            member = self._live_members.get((uid, timeframe))
            if member is not None:
                member.setdefault("lanes", {})[lane] = status

    def pair_status(self, instrument, timeframe):
        """Read-only факты готовности пары для анализа, допуска и диагностики."""
        with self._lock:
            uid = self._uids.get(self._key(instrument, timeframe))
            member = self._live_members.get((uid, timeframe)) if uid else None
            if member is None:
                return {"state": "BOOTSTRAPPING", "uid": uid, "reason": "Пара ещё не зарегистрирована ingestion"}
            state = self._refresh_member_state(uid, timeframe, member)
            cursors = self._market_store.cursors(source=self._market_source, instrument_uid=uid, interval=timeframe)
            held = self._market_store.live_state(kind="uid", source=self._market_source, instrument_uid=uid)
            return {"state": state, "uid": uid, "transport": member.get("transport") or "polling",
                    "epoch": member.get("epoch"), "history": member["history"],
                    "history_error": member["history_error"], "gap": member.get("gap"),
                    "received_through": cursors.get("received_through"),
                    "resolved_through": cursors.get("resolved_through"),
                    "processed_through": cursors.get("processed_through"),
                    "incident": held.get("incident"),
                    "cutover": None if self.live_floor(uid, timeframe) is None else self._live_cutover.isoformat(),
                    "pre_cutover_gaps": self._pre_cutover_gaps.get((uid, timeframe), 0),
                    "lanes": {lane: dict(value) for lane, value in (member.get("lanes") or {}).items()}}

    def input_ready_at(self, instrument, timeframe, open_time):
        """Момент durable-публикации текущей версии свечи (UTC-naive) либо None."""
        uid = self._uids.get(self._key(instrument, timeframe))
        if not uid or self._market_store is None:
            return None
        facts = self._market_store.live_state(kind="candle", source=self._market_source, instrument_uid=uid,
                                              interval=timeframe, open_time=open_time)
        value = facts.get("input_ready_at")
        return None if not value else pd.Timestamp(value).tz_convert("UTC").tz_localize(None)

    def execution_ready(self, instrument, through):
        """Критический 1m барьер UID: доказанный префикс исполнения до ``through``."""
        status = self.pair_status(instrument, "1m")
        if status["state"] in {"INTEGRITY_HOLD", "BOOTSTRAPPING"}:
            return False
        uid = status.get("uid")
        floor = self.live_floor(uid, "1m") if uid else None
        if floor is not None and _naive(through) < floor:
            # Вариант «б»: минуты до запуска без обязательств доказать нельзя
            # и не нужно — исполнение начнётся только после cutover.
            return True
        resolved = status.get("resolved_through")
        if resolved is None:
            return False
        return pd.Timestamp(resolved).tz_convert("UTC").tz_localize(None) >= _naive(through)

    def expected_live_open(self, key, now):
        """Открытие последнего ожидаемого закрытого бара пары по календарю."""
        grid = self._timeline.grid(key[2])
        reconciler = self._market_reconciler
        uid = self._uids.get(key)
        expectation = None if not uid or reconciler is None else reconciler.calendar_expectation(
            instrument_uid=uid, interval=key[2], now=now)
        if expectation is not None and expectation.phase.value != "unknown":
            due = expectation.last_due
            return None if due is None else _naive(due.open)

        # UNKNOWN сохраняет прежнюю сеточную семантику и частоту опроса.
        expected = _naive(grid.previous_candle_start(now))

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

    # Совместимое имя для существующих вызовов/тестов live-session.
    _expected_live_open = expected_live_open

    def _source_kwargs(self) -> dict:
        """Аргументы источника данных для загрузчика.

        Боевой режим ничего не добавляет: загрузчик сам берёт системное время и
        провайдер по умолчанию. Исторический прогон передаёт свой источник и
        виртуальные часы, иначе границы дозагрузки считались бы от «сейчас».
        """
        if not self._history:
            return {}
        return {"client_provider": self._client_provider, "clock": self._clock}

    def _load(self, instrument, timeframe: str, start_date=None, end_date=None, *, wait_for_budget=True,
              pace=True, fences=None) -> pd.DataFrame:
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
                    "clock": self._timeline.clock, "pace": pace} if self.live_session_enabled else {}),
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
                if self._owned_live_ingestion:
                    # fences вычислены вызывающим ДО начала согласующего RPC.
                    self.accept_rpc_frame(instrument_id, timeframe, df, received_at=self._timeline.now(), fences=fences)
                    with self._lock:
                        current = self._frames[key].copy()
                    if self._market_reconciler is not None:
                        # Доказательства пропусков между полученными свечами
                        # (pending/unresolved) экспортируются как прежде.
                        self._market_reconciler.reconcile(instrument_uid=instrument_id, interval=timeframe,
                                                          candles=current, now=self._timeline.now())
                    return current
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
        if self._owned_live_ingestion:
            # Чтение торгового тика: копия из памяти, никакой сети. Неизвестная
            # пара регистрируется фоновому worker. Получение не обрезается
            # из-за старой дыры: исполнительный барьер проверяет
            # contiguous_after, анализ — coverage_for необходимого хвоста.
            with self._lock:
                key = self._key(instrument, timeframe)
                uid = self._uids.get(key)
                if self.live_session_enabled and (uid is None or (uid, timeframe) not in self._live_members):
                    self._instruments.setdefault(key, instrument)
                    previous = self._pending_live_history.get(key, (None, 0))[1]
                    self._pending_live_history[key] = (instrument, max(1, previous))
                frame = self._frames.get(key)
                return pd.DataFrame() if frame is None or frame.empty else self._closed_only(frame, timeframe)
        return self._frame_for_legacy(instrument, timeframe)

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
        owned_barrier = self._owned_live_ingestion and self._market_store is not None
        if (self._market_reconciler is None and not owned_barrier) or frame.empty or timeframe != "1m" or last_bar is None:
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
                # Live-барьер действует и без календаря: тогда состояние
                # интервала — только доказательство из рыночного хранилища.
                state = (self._market_reconciler.interval_state(
                    instrument_uid=instrument_id, interval=timeframe,
                    open_time=probe, now=self._timeline.now(),
                ) if self._market_reconciler is not None else self._market_store.coverage_state(
                    source=self._market_source, instrument_uid=instrument_id, interval=timeframe, open_time=probe))
                if state not in RESOLVED_INTERVAL_STATES:
                    self._report_coverage_block(key, probe, state)
                    return frame.loc[allowed].copy()
                probe += pd.Timedelta(minutes=1)
            allowed.append(index)
            previous = stamp
        return frame.loc[allowed].copy()

    def source_absent_between(self, instrument, last_bar, frame) -> list:
        """Минуты «у источника нет» между границей исполнения и последним рядом кадра."""
        if frame is None or frame.empty or last_bar is None or self._market_store is None:
            return []
        uid = self._uids.get(self._key(instrument, "1m"))
        if not uid:
            return []
        lower = _naive(last_bar) + pd.Timedelta(minutes=1)
        upper = _naive(frame["datetime"].max())
        if upper < lower:
            return []
        snapshot = self._market_store.window_snapshot(source=self._market_source, instrument_uid=uid,
                                                      interval="1m", lower=lower, upper=upper)
        return sorted((stamp, evidence) for stamp, (state, evidence) in snapshot["states"].items()
                      if state == "source_absent" and stamp not in snapshot["received"])

    def drain_late_source_absent(self) -> list:
        """Поздние свечи минут «у источника нет»: (uid, минута, (open, high, low, close))."""
        items = []
        while self._late_after_source_absent:
            items.append(self._late_after_source_absent.popleft())
        return items

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
        if self._owned_live_ingestion:
            self.request_live_history(instrument, timeframe, 1)
            return
        return self._ensure_loaded_legacy(instrument, timeframe)

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
        if self._owned_live_ingestion:
            return
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

    def refresh_live(self):
        """В live сеть принадлежит ingestion-полосе: торговый вызов ничего не грузит."""
        return None

    def prepare_live_history(self, instrument, timeframe, min_bars, *, max_close=None):
        """Read-only кадр прогрева; недостаток регистрируется фоновому worker."""
        frame = self.request_live_history(instrument, timeframe, min_bars)
        if max_close is not None and not frame.empty:
            grid = self._timeline.grid(timeframe)
            frame = frame[frame["datetime"].apply(lambda stamp: grid.bar_close(stamp) <= max_close)]
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
        floor = self.live_floor(uid, timeframe) if self._owned_live_ingestion else None
        unconfirmed = 0
        for left, right in zip(stamps, stamps[1:]):
            probe = _naive(left) + period
            gap_unconfirmed = False
            while probe < _naive(right):
                state = self._market_reconciler.interval_state(instrument_uid=uid,
                    interval=timeframe, open_time=probe, now=self._timeline.now())
                if state not in RESOLVED_INTERVAL_STATES:
                    if floor is None or probe >= floor:
                        return False
                    # До запуска: доказательства нет и получить его нельзя —
                    # пробел виден в диагностике, но расчёт не блокирует.
                    gap_unconfirmed = True
                probe += period
            unconfirmed += gap_unconfirmed
        if floor is not None:
            self._pre_cutover_gaps[uid, timeframe] = unconfirmed
        return True

    def chart_breaks(self, instrument, timeframe, frame) -> dict:
        """Вид каждого разрыва окна для графика сделки.

        Ключ — (открытие свечи перед разрывом, открытие свечи после), UTC-naive;
        значение — ``{"kind", "missing"}``. Источник — долговечное покрытие и
        уже известный календарь того же рыночного слоя. Без сети, без записи
        и без влияния на готовность: при любой неясности разрыв остаётся
        ``schedule_unavailable``, а не пропуском и не полными данными.
        """
        if frame is None or frame.empty:
            return {}
        grid = self._timeline.grid(timeframe)
        stamps = sorted({_naive(value) for value in frame["datetime"]})
        gaps = {}
        for left, right in zip(stamps, stamps[1:]):
            probes, probe = [], _naive(grid.bar_close(left))
            while probe < right:
                probes.append(probe)
                following = _naive(grid.bar_close(probe))
                if following <= probe:
                    break
                probe = following
            if probes:
                gaps[left, right] = probes
        unknown = {key: {"kind": "schedule_unavailable", "missing": len(probes)} for key, probes in gaps.items()}
        uid = self._uids.get(self._key(instrument, timeframe))
        if not gaps or not uid or self._market_store is None or self._market_reconciler is None:
            return unknown
        try:
            window = self._market_store.window_snapshot(source=self._market_source, instrument_uid=uid,
                                                        interval=timeframe, lower=stamps[0], upper=stamps[-1])
            calendars = self._market_reconciler.stored_calendars(uid, self._timeline.now())
        except Exception as exc:
            log.warning("График %s %s: покрытие окна недоступно (%s).", uid, timeframe, type(exc).__name__)
            return unknown

        def kind(probe):
            state = window["states"].get(probe, (None, None))[0]
            if state in _CHART_RESOLVED_KINDS:
                return _CHART_RESOLVED_KINDS[state]
            calendar = next((item for item in calendars if item.covers(probe)), None)
            if calendar is None:
                return "schedule_unavailable"
            closed = _naive(grid.bar_close(probe))
            return "missing" if any(start < closed and probe < end for start, end in calendar.intervals) else "closed"

        return {key: {"kind": max((kind(probe) for probe in probes), key=_CHART_BREAK_ORDER.index),
                      "missing": len(probes)} for key, probes in gaps.items()}

    def calendar_for(self, instrument):
        uid = self._uids.get(self._key(instrument, "1m")) or getattr(instrument, "uid", None)
        return self._market_reconciler.session_calendar(uid, self._timeline.now()) if uid and self._market_reconciler else None

    @property
    def market_calendar(self):
        """Календарь прогона или live-owner'а (для отчёта и диагностики)."""
        calendar = self._market_calendar
        if calendar is None and self._market_reconciler is not None:
            calendar = getattr(self._market_reconciler, "market_calendar", None)
        return calendar

    def instrument_uid(self, instrument, timeframe="1m"):
        """UID пары из live-регистрации, иначе из карточки инструмента."""
        return self._uids.get(self._key(instrument, timeframe)) or getattr(instrument, "uid", None)

    def pair_uid(self, instrument, timeframe="1m"):
        """Адрес рыночной пары для торговой стороны: UID, даже если в карточке его нет.

        Live-выбор строит инструменты без UID, а пара сохранённого состава
        зарегистрирована под UID раньше выбора — её находим по тикеру и типу.
        """
        uid = self.instrument_uid(instrument, timeframe)
        if uid:
            return uid
        with self._lock:
            found = {pair_uid for (pair_uid, tf), member in self._live_members.items()
                     if tf == timeframe and member["instrument"].ticker == instrument.ticker
                     and member["instrument"].instrument_type == instrument.instrument_type}
        return found.pop() if len(found) == 1 else None

    def calendar_expectation(self, instrument, timeframe, now=None):
        uid = self.instrument_uid(instrument, timeframe)
        if not uid or self._market_reconciler is None:
            return None
        return self._market_reconciler.calendar_expectation(
            instrument_uid=uid, interval=timeframe, now=now or self._timeline.now()
        )

    def expected_calendar_bars(self, instrument, timeframe, lower, upper):
        uid = self.instrument_uid(instrument, timeframe)
        if not uid or self._market_reconciler is None:
            return ()
        return self._market_reconciler.expected_bars(
            instrument_uid=uid, interval=timeframe, lower=lower, upper=upper
        )

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
        expectation = self.calendar_expectation(instrument, "1m", now)
        if expectation is not None:
            phase = expectation.phase.value
            if phase == "break":
                return {"valid": False, "reason": "Торговая сессия на перерыве"}
            if phase == "closed":
                return {"valid": False, "reason": "Торговая сессия закрыта"}
            due = expectation.last_due
            if phase == "trading" and due is not None:
                actual_close = _naive(self._timeline.grid("1m").bar_close(stamp))
                if actual_close < due.close and now >= due.publication_deadline:
                    return {"valid": False, "reason": "Не получена ожидаемая базовая свеча"}
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

        def due_open(key):
            """История следует закреплённому расписанию; UNKNOWN остаётся на сетке."""
            if not self._history:
                return expected
            uid = self._uids.get(key)
            if not uid:
                return expected
            if self._market_reconciler is not None:
                expectation = self._market_reconciler.calendar_expectation(
                    instrument_uid=uid, interval=timeframe, now=now,
                )
            elif self._market_calendar is not None:
                from src.market_calendar.rules import candle_rule
                from datetime import timedelta
                self._market_calendar.service(now)
                expectation = self._market_calendar.expectation(uid, candle_rule(timeframe), now, timedelta(seconds=65))
            else:
                return expected
            if expectation is None or getattr(getattr(expectation, "phase", None), "value", None) == "unknown":
                return expected
            return None if expectation.last_due is None else _naive(expectation.last_due.open)
        if self._freshness_tolerance_bars <= 0:
            frames = [
                (key, frame)
                for key, frame in self._frames.items()
                if key[2] == timeframe and self._active_key(key)
            ]
            if not frames:
                return True
            for key, frame in frames:
                pair_expected = due_open(key)
                if pair_expected is None:
                    continue
                if frame is None or frame.empty:
                    continue
                closed = self._closed_only(frame, timeframe)
                if closed.empty or _naive(closed["datetime"].max()) < pair_expected:
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
            pair_expected = due_open(key)
            if pair_expected is None:
                continue
            threshold = pair_expected - tol
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
            if last < pair_expected:
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
                    if state not in RESOLVED_INTERVAL_STATES:
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
