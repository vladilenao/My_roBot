"""Фоновая unary-полоса live-ingestion: прогрев, свежая дозагрузка и recovery.

Отдельный модуль нужен из-за отсутствующего контракта: сеть, повторы и
планирование RPC вынесены из торгового потока и owner-lock (design 2, 6.2).
Кэш остаётся единственным владельцем принятия; worker только выбирает
следующий ограниченный RPC и передаёт страницу в accept-путь кэша.

Один вызов ``step`` выполняет не больше одной единицы работы, поэтому stop,
fallback и stream-reader не ждут длинной истории.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from src.data.ingestion_settings import MarketDataSettings
from src.data.request_budget import RequestDeferred
from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)

WARMUP_DAYS = (30, 60, 120, 240, 480, 960)

# Консервативный span одного GetCandles (design 8): не больше SDK
# MAX_INTERVALS и документированных пределов D1; overlap входит внутрь span.
CHUNK_SPAN = {
    "1m": timedelta(days=1), "5m": timedelta(days=1), "15m": timedelta(days=1), "30m": timedelta(days=1),
    "1h": timedelta(days=7), "4h": timedelta(days=7),
    "1d": timedelta(days=365), "1w": timedelta(days=365), "1M": timedelta(days=1095),
}


def _naive(value):
    return None if value is None else to_naive(value)


def chunk_windows(timeframe, start, end, grid):
    """Перекрывающиеся окна ``[lower, upper)`` полного диапазона.

    Соседние окна перекрываются одним нативным периодом, поэтому свеча ровно
    на стыке проверяется при любой включённости верхней границы источника.
    """
    span = CHUNK_SPAN.get(timeframe)
    if span is None:
        raise ValueError(f"Нет span GetCandles для ТФ {timeframe!r}")
    lower, end = _naive(start), _naive(end)
    windows = []
    while lower < end:
        upper = min(lower + span, end)
        windows.append((lower, upper))
        if upper >= end:
            break
        overlap = _naive(grid.current_candle_start(upper - pd.Timedelta(microseconds=1)))
        lower = overlap if lower < overlap < upper else upper
    return windows


@dataclass
class RecoveryRange:
    """Сохраняемая область восстановления одной пары (design 7, 7a)."""

    lower: pd.Timestamp
    upper: pd.Timestamp
    round_started_at: pd.Timestamp
    next_retry_at: pd.Timestamp
    checkpoint: pd.Timestamp
    attempts: int = 0
    state: str = "active"
    reason: str = ""

    def as_json(self):
        return {"lower": self.lower.isoformat(), "upper": self.upper.isoformat(),
                "round_started_at": self.round_started_at.isoformat(),
                "next_retry_at": self.next_retry_at.isoformat(), "checkpoint": self.checkpoint.isoformat(),
                "attempts": self.attempts, "state": self.state, "reason": self.reason}

    @classmethod
    def from_json(cls, value):
        if not value or "lower" not in value:
            return None
        return cls(*(pd.Timestamp(value[key]) for key in ("lower", "upper", "round_started_at", "next_retry_at", "checkpoint")),
                   attempts=int(value.get("attempts", 0)), state=value.get("state", "active"),
                   reason=value.get("reason", ""))


class IngestionWorker:
    """Планировщик ограниченных unary RPC одного владельца входа."""

    def __init__(self, cache, *, settings: MarketDataSettings | None = None):
        if not getattr(cache, "live_session_enabled", False) or cache._market_store is None:
            raise ValueError("IngestionWorker требует live-кэш с рыночным хранилищем")
        self._cache = cache
        self._store = cache._market_store
        self._source = cache._market_source
        self._reconciler = cache._market_reconciler
        self._budget = cache._request_budget
        self._timeline = cache._timeline
        self.settings = settings or MarketDataSettings()
        self._fresh = deque()
        self._recovery_due = False
        self._aux_at = pd.Timestamp.min
        self._aux_turn = 0
        self._recent_at = {}
        self._recovery_cursor = 0
        self._old_queue = deque()
        self._conflict_at = {}
        self._fresh_at = {}
        self._need_attempts = {}
        self._need_retry_at = {}
        self._detect_at = pd.Timestamp.min
        self._attempted = set()
        self.errors = {}
        # Пары, чьё оперативное получение сейчас обеспечивает stream, и пары,
        # удержанные без polling (fallback запрещён настройкой).
        self._stream_pairs = set()
        self._held_pairs = set()
        self._handoffs = {}

    # ── вход supervisor ──
    def set_stream_pairs(self, pairs, *, held=()):
        self._stream_pairs = set(pairs)
        self._held_pairs = set(held)

    def request_handoff(self, uid, timeframe, epoch, *, acknowledged_at, on_ready):
        """После ACK: историческое перекрытие до фиксированного H, затем READY.

        H — граница начала согласования; более новые события stream уже
        принимаются owner и не сдвигают цель бесконечно (design 3).
        """
        key = self._pair_key(uid, timeframe)
        if key is None:
            return
        horizon = self._cache.expected_live_open(key, _naive(acknowledged_at))
        self._handoffs[uid, timeframe] = (epoch, horizon, on_ready)

    def cancel_handoff(self, uid, timeframe):
        self._handoffs.pop((uid, timeframe), None)

    def note_loss(self, uid, timeframe, since, *, reason="delivery_loss"):
        """Известный обрыв/переполнение: range recovery без ожидания deadline."""
        now = _naive(self._timeline.now())
        key = self._pair_key(uid, timeframe)
        if key is None:
            return
        horizon = self._cache.expected_live_open(key, now)
        if horizon is None:
            return
        lower = min(_naive(since), horizon)
        self._merge_range(uid, timeframe, lower, horizon, now, reason=reason, immediate=True)

    def startup_complete(self):
        """Каждая потребность хотя бы раз обслужена; READY решает кэш.

        Неудачная попытка тоже завершает ожидание старта: пара остаётся
        неготовой с видимой причиной, а повторы идут в фоне с backoff.
        """
        if any(key not in self._need_attempts for key, _, _ in self._cache.live_needs()):
            return False
        return all(pair["history"] == "done" or (pair["uid"], pair["timeframe"]) in self._attempted
                   for pair in self._cache.live_pairs())

    # ── одна единица работы ──
    def step(self, now=None):
        now = _naive(now or self._timeline.now())
        if self._budget is not None and self._budget.retry_after is not None and now < self._budget.retry_after:
            return False
        try:
            if self._service_need(now):
                return True
            if self._service_handoff(now):
                return True
            # После оборота свежей очереди — одна auxiliary задача; ни один
            # вид работы не голодает и не блокирует другой.
            auxiliary = self._recovery_due or (not self._fresh and now >= self._aux_at)
            if auxiliary:
                self._recovery_due = False
                self._aux_at = now + timedelta(seconds=self.settings.recovery_recheck_interval_secs)
                return self._service_auxiliary(now) or self._service_fresh(now)
            return self._service_fresh(now) or self._service_auxiliary(now)
        except RequestDeferred:
            return False

    def run_until_idle(self, now=None, limit=10000):
        """Тестовый/стартовый синхронный проход без сна."""
        for _ in range(limit):
            if not self.step(now):
                return True
        return False

    # ── потребности прогрева и регистрации ──
    def _service_need(self, now):
        for key, instrument, min_bars in self._cache.live_needs():
            if now < self._need_retry_at.get(key, pd.Timestamp.min):
                continue
            timeframe = key[2]
            uid = self._cache._uids.get(key) or getattr(instrument, "uid", None)
            if not uid:
                self._need_attempts[key] = self._need_attempts.get(key, 0) + 1
                uid = self._resolve_uid(instrument)
                if uid is None:
                    self._need_retry_at[key] = now + self._backoff(self._need_attempts[key])
                    continue
            try:
                self._cache.register_live_pair(instrument, uid, timeframe, min_bars)
            except ValueError as exc:
                # Неоднозначный alias UID: данные не смешиваются, пара
                # удерживается неготовой с причиной, без быстрого повтора.
                self._cache._live_errors[key] = str(exc)
                self._need_attempts[key] = self._need_attempts.get(key, 0) + 1
                self._cache.finish_live_need(key, min_bars)
                log.warning("Live-пара %s удержана: %s", key, exc)
                continue
            self._need_attempts.setdefault(key, 0)
            pair = self._pair(uid, timeframe)
            if pair["closed"] >= min_bars and pair["history"] == "done":
                self._cache.finish_live_need(key, min_bars)
                continue
            if pair["closed"] >= min_bars and (pair["warmup_days"] or pair["closed"]):
                self._cache.mark_live_history(uid, timeframe, done=True)
                self._cache.finish_live_need(key, min_bars)
                continue
            days = next((value for value in WARMUP_DAYS if value > pair["warmup_days"]), None)
            if days is None:
                self._cache.mark_live_history(uid, timeframe, done=True,
                    error=f"Недостаточно истории: {pair['closed']}/{min_bars} свечей")
                self._cache.finish_live_need(key, min_bars)
                continue
            self._attempted.add((uid, timeframe))
            self._need_attempts[key] = self._need_attempts.get(key, 0) + 1
            earliest = self._earliest(uid, timeframe)
            try:
                self._load_range(instrument, timeframe, now - timedelta(days=days), now, wait=True)
            except RequestDeferred:
                raise
            except Exception as exc:
                self.errors[uid, timeframe] = str(exc)
                self._cache.mark_live_history(uid, timeframe, error=str(exc))
                self._need_retry_at[key] = now + self._backoff(self._need_attempts[key])
                log.warning("Прогрев %s %s: %s", uid, timeframe, exc)
                return True
            self._cache.mark_live_history(uid, timeframe, days=days)
            pair = self._pair(uid, timeframe)
            if pair["closed"] >= min_bars:
                self._cache.mark_live_history(uid, timeframe, done=True)
                self._cache.finish_live_need(key, min_bars)
            elif pair["warmup_days"] > 30 and earliest is not None and self._earliest(uid, timeframe) == earliest:
                # Расширение не дало ни одной более ранней свечи: достигнут
                # предел доступной истории, дальнейшие окна не запрашиваются.
                self._cache.mark_live_history(uid, timeframe, done=True,
                    error=f"Недостаточно истории: {pair['closed']}/{min_bars} свечей")
                self._cache.finish_live_need(key, min_bars)
            return True
        return False

    def _service_handoff(self, now):
        for (uid, timeframe), (epoch, horizon, on_ready) in list(self._handoffs.items()):
            pair = self._pair(uid, timeframe)
            if pair is None or pair["history"] != "done":
                continue
            grid = self._timeline.grid(timeframe)
            if horizon is not None:
                last = pair["last_closed"]
                cursor = self._store.cursors(source=self._source, instrument_uid=uid, interval=timeframe)
                resolved = cursor.get("resolved_through")
                lower = (pd.Timestamp(resolved).tz_convert("UTC").tz_localize(None) if resolved is not None
                         else last if last is not None else horizon)
                lower = min(_naive(lower), horizon)
                floor = self._cache.live_floor(uid, timeframe)
                if floor is not None:
                    lower = min(max(lower, floor), horizon)
                try:
                    self._load_range(pair["instrument"], timeframe, lower, grid.bar_close(horizon))
                except RequestDeferred:
                    raise
                except Exception as exc:
                    # Пара остаётся в RECONCILING; приём stream продолжается.
                    self.errors[uid, timeframe] = str(exc)
                    return True
            self._handoffs.pop((uid, timeframe), None)
            on_ready(uid, timeframe, epoch)
            return True
        return False

    def _earliest(self, uid, timeframe):
        frame = self._cache._closed_member_frame(self._cache._live_members[uid, timeframe], timeframe)
        return None if frame.empty else frame["datetime"].min()

    def _resolve_uid(self, instrument):
        from src.data.loader import resolve_instrument_id
        try:
            return resolve_instrument_id(ticker=instrument.ticker, instrument_type=instrument.instrument_type,
                token=self._cache._token, client_provider=self._cache._client_provider,
                request_budget=self._budget, clock=self._timeline.clock)
        except RequestDeferred:
            raise
        except Exception as exc:
            log.warning("Не удалось узнать UID %s из каталога: %s", instrument.ticker, exc)
            return None

    def _load_range(self, instrument, timeframe, start, end, *, wait=False, fences=None):
        """Полный диапазон чанками; каждый успешный чанк сразу durable."""
        grid = self._timeline.grid(timeframe)
        windows = chunk_windows(timeframe, start, end, grid)
        for index, (lower, upper) in enumerate(windows):
            self._cache._load(instrument, timeframe, start_date=lower, end_date=upper,
                              wait_for_budget=wait, pace=index == 0, fences=fences)
        return windows

    # ── свежий polling: прежняя справедливая очередь live-session ──
    def _polled(self, pair):
        identity = (pair["uid"], pair["timeframe"])
        return identity not in self._stream_pairs and identity not in self._held_pairs

    def _service_fresh(self, now):
        if not self._fresh:
            candidates = []
            for pair in self._cache.live_pairs():
                if pair["history"] != "done" or not self._polled(pair):
                    continue
                expected = self._cache.expected_live_open(pair["key"], now)
                if expected is None:
                    continue
                last = pair["last_closed"] if pair["last_closed"] is not None else pd.Timestamp.min
                if last >= expected:
                    continue
                # Тот же ожидаемый бар повторно — не чаще ритма публикации.
                previous = self._fresh_at.get(pair["key"])
                if previous is not None and previous[0] == expected and now < previous[1]:
                    continue
                candidates.append((last, pair["key"], pair["uid"], pair["timeframe"]))
            self._fresh.extend(sorted(candidates))
        if not self._fresh:
            return False
        last, key, uid, timeframe = self._fresh.popleft()
        expected = self._cache.expected_live_open(key, now)
        if expected is None:
            return True
        instrument = self._pair(uid, timeframe)["instrument"]
        self._fresh_at[key] = (expected, now + timedelta(seconds=max(1.0, float(self._cache._data_refresh_min_interval or 0))))
        # Скользящее окно в час не должно исключать последнюю H1-свечу
        # (в 09:30 её open_time=08:00, а now-window=08:30).
        start = min(self._cache._incremental_start(None if last == pd.Timestamp.min else last, now) or expected, expected)
        calendar = self._cache.calendar_for(instrument)
        end = self._timeline.grid(timeframe).bar_close(expected) if (
            calendar is not None and calendar.covers(now) and not calendar.is_open(now)) else None
        try:
            self._cache._load(instrument, timeframe, start_date=start, end_date=end, wait_for_budget=False)
            self._cache._live_errors.pop(key, None)
        except RequestDeferred:
            self._fresh.appendleft((last, key, uid, timeframe))
            raise
        except Exception as exc:
            self._cache._live_errors[key] = str(exc)
            log.warning("Обновление рыночных данных %s: %s", key, exc)
        if not self._fresh:
            self._recovery_due = True
        return True

    # ── auxiliary: recovery, сверка, конфликты, календарь по очереди ──
    def _service_auxiliary(self, now):
        kinds = (self._service_recovery, self._service_old_gap, self._service_recent,
                 self._service_conflicts, self._service_calendar)
        for offset in range(len(kinds)):
            index = (self._aux_turn + offset) % len(kinds)
            if kinds[index](now):
                self._aux_turn = index + 1
                return True
        return False

    def _service_old_gap(self, now):
        """Старейший несвязанный пробел: одна ограниченная попытка с cooldown.

        Это прежний live-session бюджет для пропусков вне необходимого
        префикса; он не подменяет range recovery и свежие запросы.
        """
        if self._reconciler is None:
            return False
        if not self._old_queue:
            self._old_queue.extend(sorted((pair["uid"], pair["timeframe"]) for pair in self._cache.live_pairs()
                                          if pair["history"] == "done"))
        while self._old_queue:
            uid, timeframe = self._old_queue.popleft()
            pair = self._pair(uid, timeframe)
            if pair is None:
                continue
            self._reconciler.recheck(uid, timeframe, now)
            gap = self._reconciler.recovery_candidate(instrument_uid=uid, interval=timeframe, now=now)
            if gap is None or gap >= now:
                continue
            if self._reconciler.interval_state(instrument_uid=uid, interval=timeframe, open_time=gap, now=now) == "scheduled_closed":
                continue
            period = self._timeline.grid(timeframe).bar_close(gap) - gap
            attempted = True
            try:
                before = self._store.coverage_state(source=self._source, instrument_uid=uid, interval=timeframe, open_time=gap)
                self._cache._load(pair["instrument"], timeframe, start_date=gap, end_date=gap + period, wait_for_budget=False)
                after = self._store.coverage_state(source=self._source, instrument_uid=uid, interval=timeframe, open_time=gap)
                if after != "received" and after == before:
                    self._reconciler.record_empty_recovery(instrument_uid=uid, interval=timeframe, open_time=gap)
            except RequestDeferred:
                attempted = False
                self._old_queue.appendleft((uid, timeframe))
                raise
            except Exception as exc:
                self.errors[uid, timeframe] = str(exc)
            finally:
                # У фактического RPC свой durable cooldown, даже при пустом ответе.
                if attempted:
                    self._store.record_recovery_attempt(source=self._source, instrument_uid=uid, interval=timeframe,
                        open_time=gap, now=now, next_attempt=now + timedelta(seconds=self.settings.recovery_recheck_interval_secs))
            return True
        return False

    def _service_calendar(self, now):
        return bool(self._reconciler is not None and self._reconciler.service_calendar(now))

    def _service_conflicts(self, now):
        for uid, timeframe, stamp in self._cache.live_conflicts():
            pair = self._pair(uid, timeframe)
            if pair is None or now < self._conflict_at.get((uid, timeframe, stamp), pd.Timestamp.min):
                continue
            # Временный конфликт не превращается в бесконечный tight retry.
            self._conflict_at[uid, timeframe, stamp] = now + self._backoff(2)
            period = self._timeline.grid(timeframe).bar_close(stamp) - stamp
            frame = pd.DataFrame({"datetime": [stamp]})
            fences = self._cache.reconciliation_fences(uid, timeframe, frame)
            try:
                self._cache._load(pair["instrument"], timeframe, start_date=stamp, end_date=stamp + period,
                                  wait_for_budget=False, fences=fences)
            except RequestDeferred:
                raise
            except Exception as exc:
                self.errors[uid, timeframe] = str(exc)
            return True
        return False

    def _service_recent(self, now):
        """Недорогая сверка последних закрытых баров без burst всего roster."""
        interval = timedelta(seconds=self.settings.recent_reconciliation_interval_secs)
        for pair in self._cache.live_pairs():
            if pair["history"] != "done" or pair["last_closed"] is None:
                continue
            identity = (pair["uid"], pair["timeframe"])
            if identity not in self._recent_at:
                # Первая сверка через интервал, распределённо по окну вместо
                # одновременного burst всего roster при запуске.
                spread = (hash(identity) % 1000) / 1000 * interval.total_seconds()
                self._recent_at[identity] = now + interval + timedelta(seconds=spread)
            if now < self._recent_at[identity]:
                continue
            self._recent_at[identity] = now + interval
            grid = self._timeline.grid(pair["timeframe"])
            last = pair["last_closed"]
            stamps = [last]
            for _ in range(self.settings.recent_reconciliation_bars - 1):
                stamps.insert(0, _naive(grid.previous_candle_start(stamps[0])))
            start = stamps[0]
            fences = self._cache.reconciliation_fences(pair["uid"], pair["timeframe"], pd.DataFrame({"datetime": stamps}))
            try:
                self._cache._load(pair["instrument"], pair["timeframe"], start_date=start,
                                  end_date=grid.bar_close(last), wait_for_budget=False, fences=fences)
            except RequestDeferred:
                self._recent_at[identity] = now
                raise
            except Exception as exc:
                self.errors[identity] = str(exc)
            return True
        return False

    # ── ожидаемая последовательность и range recovery ──
    def detect(self, now=None):
        """Проверка всего необходимого префикса и хвоста каждой пары."""
        now = _naive(now or self._timeline.now())
        for pair in self._cache.live_pairs():
            if pair["history"] != "done":
                continue
            self._clip_to_floor(pair)
            missing, horizon = self._first_missing(pair, now)
            if missing is not None:
                self._merge_range(pair["uid"], pair["timeframe"], missing, horizon, now)
            else:
                book = self._load_book(pair["uid"], pair["timeframe"])
                if book is not None and book.upper <= (horizon or book.upper):
                    self._clear_range(pair["uid"], pair["timeframe"])

    def _clip_to_floor(self, pair):
        """Сохранённая область прошлого запуска не уходит за cutover."""
        floor = self._cache.live_floor(pair["uid"], pair["timeframe"])
        book = self._load_book(pair["uid"], pair["timeframe"]) if floor is not None else None
        if book is None or book.lower >= floor:
            return
        if book.upper < floor:
            return self._clear_range(pair["uid"], pair["timeframe"])
        book.lower, book.checkpoint = floor, max(book.checkpoint, floor)
        self._save_book(pair["uid"], pair["timeframe"], book)

    def _horizon(self, key, now):
        """Последний ожидаемый закрытый интервал, чей срок публикации истёк."""
        shifted = now - timedelta(seconds=self.settings.candle_publication_timeout_secs)
        return self._cache.expected_live_open(key, shifted)

    def _first_missing(self, pair, now, lower=None, horizon=None):
        uid, timeframe = pair["uid"], pair["timeframe"]
        horizon = horizon if horizon is not None else self._horizon(pair["key"], now)
        if horizon is None:
            return None, None
        grid = self._timeline.grid(timeframe)
        if lower is None:
            cursor = self._store.cursors(source=self._source, instrument_uid=uid, interval=timeframe)
            resolved = cursor.get("resolved_through")
            if resolved is not None:
                lower = _naive(grid.bar_close(pd.Timestamp(resolved).tz_convert("UTC").tz_localize(None)))
            else:
                state = self._store.live_state(kind="pair", source=self._source, instrument_uid=uid, interval=timeframe)
                if not state.get("coverage_lower"):
                    return None, horizon
                lower = _naive(state["coverage_lower"])
        floor = self._cache.live_floor(uid, timeframe)
        if floor is not None:
            lower = max(_naive(lower), floor)  # вариант «б»: не раньше запуска
        probe, steps = _naive(lower), 0
        frame = self._cache._closed_member_frame(self._cache._live_members[uid, timeframe], timeframe)
        received = set(frame.loc[frame["datetime"] >= probe, "datetime"]) if not frame.empty else set()
        while probe <= horizon and steps < 200000:
            steps += 1
            if probe in received:
                probe = _naive(grid.bar_close(probe))
                continue
            jump = self._cache._resolved_jump(uid, timeframe, probe, now)
            if jump is None:
                return probe, horizon
            probe = jump
        return None, horizon

    def _pair_key(self, uid, timeframe):
        pair = self._pair(uid, timeframe)
        return None if pair is None else pair["key"]

    def _pair(self, uid, timeframe):
        return self._cache.live_pair(uid, timeframe)

    def _load_book(self, uid, timeframe):
        return RecoveryRange.from_json(self._store.live_state(kind="recovery", source=self._source,
                                                              instrument_uid=uid, interval=timeframe))

    def _save_book(self, uid, timeframe, book):
        self._store.save_live_state({} if book is None else book.as_json(), kind="recovery",
                                    source=self._source, instrument_uid=uid, interval=timeframe)
        self._cache.set_live_gap(uid, timeframe, None if book is None else {
            "lower": book.lower.isoformat(), "upper": book.upper.isoformat(), "state": book.state,
            "attempts": book.attempts, "next_retry_at": book.next_retry_at.isoformat(), "reason": book.reason})

    def _clear_range(self, uid, timeframe):
        self._save_book(uid, timeframe, None)

    def _merge_range(self, uid, timeframe, lower, upper, now, *, reason="expected_missing", immediate=False):
        book = self._load_book(uid, timeframe)
        if book is None:
            book = RecoveryRange(lower, upper, now, now, lower, reason=reason)
        else:
            cursor = self._store.cursors(source=self._source, instrument_uid=uid, interval=timeframe)
            high_water = cursor.get("received_through")
            resumed = (high_water is not None
                       and pd.Timestamp(high_water).tz_convert("UTC").tz_localize(None) > book.upper)
            book.lower, book.upper = min(book.lower, lower), max(book.upper, upper)
            book.checkpoint = min(book.checkpoint, lower)
            if book.state == "parked" and (immediate or resumed):
                # Значимое новое доказательство — обрыв/reconnect или снова
                # публикуемые позже области свечи — открывает новый раунд.
                # Удлинение того же обрыва раунд не перезапускает.
                book.state, book.attempts, book.round_started_at = "active", 0, now
                book.next_retry_at, book.reason = now, reason
            elif immediate:
                book.next_retry_at = min(book.next_retry_at, now)
        self._save_book(uid, timeframe, book)
        self._recovery_due = True

    def _backoff(self, attempts):
        base = max(1.0, float(self._cache._data_refresh_min_interval or 1))
        return timedelta(seconds=min(self.settings.recovery_backoff_max_secs, base * (2 ** max(0, attempts - 1))))

    def _service_recovery(self, now):
        if now >= self._detect_at:
            self._detect_at = now + timedelta(seconds=max(1.0, float(self._cache._data_refresh_min_interval or 1)))
            self.detect(now)
        pairs = [pair for pair in self._cache.live_pairs() if pair["history"] == "done"]
        # Сначала причинно нужная base 1m, затем рабочие/HTF; внутри — по кругу.
        pairs.sort(key=lambda pair: (pair["timeframe"] != "1m", pair["uid"], pair["timeframe"]))
        if not pairs:
            return False
        for offset in range(len(pairs)):
            pair = pairs[(self._recovery_cursor + offset) % len(pairs)]
            book = self._load_book(pair["uid"], pair["timeframe"])
            if book is None or now < book.next_retry_at:
                continue
            self._recovery_cursor = (self._recovery_cursor + offset + 1) % len(pairs)
            self._recover_chunk(pair, book, now)
            return True
        return False

    def _round_exhausted(self, book, now):
        elapsed = (now - book.round_started_at).total_seconds()
        return book.attempts >= self.settings.recovery_max_attempts or elapsed >= self.settings.recovery_round_timeout_secs

    def _park(self, pair, book, now, missing):
        book.state = "parked"
        book.next_retry_at = now + timedelta(seconds=self.settings.recovery_recheck_interval_secs)
        if missing is not None:
            book.checkpoint = missing
            # Пустота не становится no-trade: область явно неразрешена.
            self._store.set_interval_state(source=self._source, instrument_uid=pair["uid"],
                interval=pair["timeframe"], open_time=missing, state="unresolved",
                evidence="recovery_round_exhausted", observed_at=now)
        self._save_book(pair["uid"], pair["timeframe"], book)

    def _recover_chunk(self, pair, book, now):
        uid, timeframe = pair["uid"], pair["timeframe"]
        if book.state == "active" and self._round_exhausted(book, now):
            missing, _ = self._first_missing(pair, now, lower=book.checkpoint, horizon=book.upper)
            return self._park(pair, book, now, missing)
        grid = self._timeline.grid(timeframe)
        end = _naive(grid.bar_close(book.upper))
        windows = chunk_windows(timeframe, book.checkpoint, end, grid)
        lower, upper = windows[0]
        try:
            self._cache._load(pair["instrument"], timeframe, start_date=lower, end_date=upper,
                              wait_for_budget=False, pace=False)
        except RequestDeferred:
            raise
        except Exception as exc:
            self.errors[uid, timeframe] = str(exc)
            book.attempts += 1
            book.next_retry_at = now + self._backoff(book.attempts)
            if book.state == "active" and self._round_exhausted(book, now):
                return self._park(pair, book, now, book.checkpoint)
            return self._save_book(uid, timeframe, book)
        if len(windows) > 1 and book.state == "active":
            # Чекпоинт успешного чанка переживает сбой следующего и restart.
            book.checkpoint = windows[1][0]
            book.next_retry_at = now
            return self._save_book(uid, timeframe, book)
        missing, _ = self._first_missing(pair, now, lower=book.lower, horizon=book.upper)
        if missing is None:
            return self._clear_range(uid, timeframe)
        book.checkpoint = missing
        if book.state == "parked":
            book.next_retry_at = now + timedelta(seconds=self.settings.recovery_recheck_interval_secs)
            return self._save_book(uid, timeframe, book)
        book.attempts += 1
        book.next_retry_at = now + self._backoff(book.attempts)
        if self._round_exhausted(book, now):
            return self._park(pair, book, now, missing)
        self._reconciler_empty(uid, timeframe, missing)
        return self._save_book(uid, timeframe, book)

    def _reconciler_empty(self, uid, timeframe, missing):
        if self._reconciler is not None:
            self._reconciler.record_empty_recovery(instrument_uid=uid, interval=timeframe, open_time=missing)
