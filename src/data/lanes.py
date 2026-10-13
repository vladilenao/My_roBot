"""Независимые полосы получения рыночных данных (add-independent-market-data-lanes).

Каждая полоса — одна единица работы за ``step()``, своя очередь и своя доля
лимита брокера (полоса задаётся контекстом потока у ``RequestBudget``):

- ``EvidenceLane`` — пропуски не старше окна доказательства: диапазонный
  GetCandles и сразу GetLastTrades, плюс сверка последних баров;
- ``CompletenessLane`` — дыры старше окна доказательства в окне полноты:
  догрузка диапазонами, конечное расписание проверок, затем
  ``source_absent``;
- ``CalendarLane`` — привязки и расписание на сервисе инструментов.

Живая полоса — ``IngestionWorker.step()`` (прогрев, handoff, свежий опрос).
Все наблюдения идут через владельца входа (``MarketDataCache._load``).
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from src.data.coverage_states import RESOLVED_INTERVAL_STATES
from src.data.ingestion import CHUNK_SPAN, RecoveryRange, _naive
from src.data.request_budget import RequestDeferred
from src.logging_setup import get_logger

log = get_logger(__name__)

# Последние бары рабочего ТФ на картинке сделки (src/events/visual.py).
CHART_BARS = 80
# Без данных и календаря окно N баров по сетке берётся с запасом на ночи.
GRID_SLACK = 3


class EvidenceLane:
    """Недавние пропуски: свеча или доказательство, пока GetLastTrades возможен."""

    name = "evidence"

    def __init__(self, worker, completeness=None):
        self._worker = worker
        self._cache = worker._cache
        self._timeline = worker._timeline
        self.settings = worker.settings
        # Нижняя граница нужного — окно полноты пары: тихие минуты прямо перед
        # запуском ещё доказуемы GetLastTrades, позже — только «у источника нет».
        self._completeness = completeness
        self._window = timedelta(seconds=self.settings.evidence_window_secs)
        self._retry = tuple(timedelta(seconds=value) for value in self.settings.evidence_retry_secs)
        self._next: dict[tuple[str, str], pd.Timestamp] = {}
        self._attempts: dict[tuple[str, str, pd.Timestamp], int] = {}
        self._losses: dict[tuple[str, str], pd.Timestamp] = {}
        self._cursor = 0
        self._recent_turn = False

    def note_loss(self, uid, timeframe, since):
        """Известная потеря доставки: недавняя часть — без ожидания срока публикации."""
        previous = self._losses.get((uid, timeframe))
        since = _naive(since)
        self._losses[uid, timeframe] = since if previous is None else min(previous, since)
        self._next.pop((uid, timeframe), None)

    def _bounds(self, pair, now):
        uid, timeframe = pair["uid"], pair["timeframe"]
        grid = self._timeline.grid(timeframe)
        lower = _naive(grid.current_candle_start(now - self._window))
        if lower < now - self._window:
            lower = _naive(grid.bar_close(lower))
        loss = self._losses.get((uid, timeframe))
        if loss is not None:
            # Потеря известна: проверяем до последнего закрытого бара сразу.
            horizon = self._cache.expected_live_open(pair["key"], now)
            return max(lower, _naive(loss)), horizon
        return lower, None

    def _need_lower(self, pair, now):
        if self._completeness is not None:
            return self._completeness.window_lower(pair, now)
        return self._cache.live_floor(pair["uid"], pair["timeframe"])

    def _pending_pair(self, pair, now):
        lower, horizon = self._bounds(pair, now)
        need = self._need_lower(pair, now)
        if need is not None:
            lower = max(lower, need)
        missing, horizon = self._worker._first_missing(pair, now, lower=lower, horizon=horizon,
                                                       floor=self._completeness is None)
        if missing is None:
            self._losses.pop((pair["uid"], pair["timeframe"]), None)
        return missing, horizon

    def step(self, now=None):
        now = _naive(now or self._timeline.now())
        pairs = [pair for pair in self._cache.live_pairs() if pair["history"] == "done"]
        if pairs:
            for offset in range(len(pairs)):
                pair = pairs[(self._cursor + offset) % len(pairs)]
                identity = (pair["uid"], pair["timeframe"])
                if now < self._next.get(identity, pd.Timestamp.min):
                    continue
                missing, horizon = self._pending_pair(pair, now)
                if missing is None:
                    continue
                self._cursor = (self._cursor + offset + 1) % len(pairs)
                self._check(pair, missing, horizon, now)
                return True
        # Сверка последних баров — та же работа о недавней истине.
        return bool(self._worker._service_recent(now))

    def _check(self, pair, missing, horizon, now):
        uid, timeframe = pair["uid"], pair["timeframe"]
        grid = self._timeline.grid(timeframe)
        try:
            self._cache._load(pair["instrument"], timeframe, start_date=missing,
                              end_date=_naive(grid.bar_close(horizon)), wait_for_budget=False, pace=False)
        except RequestDeferred:
            raise
        except Exception as exc:
            self._worker.errors[uid, timeframe] = str(exc)
            self._schedule(pair, missing, now)
            return
        floor = self._completeness is None
        remaining, _ = self._worker._first_missing(pair, now, lower=missing, horizon=horizon, floor=floor)
        if remaining is not None:
            self._worker._confirm_no_trades(pair, remaining, horizon, now)
            remaining, _ = self._worker._first_missing(pair, now, lower=remaining, horizon=horizon, floor=floor)
        if remaining is None:
            self._losses.pop((uid, timeframe), None)
            self._next.pop((uid, timeframe), None)
            self._cache.set_lane_status(uid, timeframe, self.name, {"missing": None, "attempts": 0,
                                                                     "next_check_at": None, "checked_at": _text(now)})
            return
        self._schedule(pair, remaining, now)

    def _schedule(self, pair, missing, now):
        """Свечи и доказательства нет: короткие повторы, пока интервал в окне."""
        key = (pair["uid"], pair["timeframe"], missing)
        attempt = self._attempts.get(key, 0)
        self._attempts[key] = attempt + 1
        delay = self._retry[min(attempt, len(self._retry) - 1)]
        self._next[pair["uid"], pair["timeframe"]] = now + delay
        self._cache.set_lane_status(pair["uid"], pair["timeframe"], self.name, {
            "missing": _text(missing), "attempts": attempt + 1, "next_check_at": _text(now + delay),
            "checked_at": _text(now)})
        # Старые счётчики интервалов, ушедших из окна, не копятся.
        horizon = now - self._window
        for stale in [item for item in self._attempts if item[2] < horizon]:
            self._attempts.pop(stale, None)


class CompletenessLane:
    """Дыры окна полноты старше окна доказательства: диапазоны и итог каждой минуты."""

    name = "completeness"

    def __init__(self, worker):
        self._worker = worker
        self._cache = worker._cache
        self._store = worker._store
        self._source = worker._source
        self._timeline = worker._timeline
        self.settings = worker.settings
        self._window = timedelta(seconds=self.settings.evidence_window_secs)
        self._rechecks = tuple(timedelta(seconds=value) for value in self.settings.completeness_recheck_secs)
        self._rescan = timedelta(seconds=self.settings.completeness_rescan_interval_secs)
        self._active: set[tuple[str, str]] | None = None
        self._skip_until: dict[tuple[str, str], pd.Timestamp] = {}
        self._cursors: dict[int, int] = {}
        self.windows: dict[tuple[str, str], pd.Timestamp] = {}
        self.last_holes: dict[tuple[str, str], int] = {}
        self._books_adopted = False

    # ── состав и окно ──
    def set_active(self, pairs):
        """Пары, которые полоса обязана держать полными; None — все зарегистрированные."""
        self._active = None if pairs is None else set(pairs)
        self._skip_until.clear()

    def wake(self):
        self._skip_until.clear()

    def window_lower(self, pair, now):
        """Нижняя граница окна полноты пары (design 5)."""
        uid, timeframe = pair["uid"], pair["timeframe"]
        grid = self._timeline.grid(timeframe)
        bars = max(int(pair["min_bars"] or 1), 1 if timeframe == "1m" else CHART_BARS)
        member = self._cache._live_members.get((uid, timeframe))
        frame = self._cache._closed_member_frame(member, timeframe) if member is not None else pd.DataFrame()
        candidates = []
        if len(frame) >= bars:
            candidates.append(_naive(frame["datetime"].iloc[-bars]))
        else:
            period = grid.bar_close(grid.current_candle_start(now)) - grid.current_candle_start(now)
            candidates.append(_naive(grid.current_candle_start(now - period * bars * GRID_SLACK)))
        if timeframe == "1m":
            obligation = self._worker.execution_boundary(pair["key"][0])
            if obligation is not None:
                stamp, inclusive = obligation
                candidates.append(stamp if inclusive else _naive(grid.bar_close(stamp)))
            floor = self._cache.live_floor(uid, timeframe)
            if floor is not None:
                candidates.append(floor)
        lower = min(candidates)
        previous = self.windows.get((uid, timeframe))
        if previous != lower:
            self.windows[uid, timeframe] = lower
            self._store.set_roster_window(source=self._source, instrument_uid=uid, interval=timeframe,
                                          window_lower=lower)
        return lower

    def _upper(self, pair, now):
        """Последний ожидаемый бар старше окна доказательства."""
        horizon = self._worker._horizon(pair, now)
        if horizon is None:
            return None
        grid = self._timeline.grid(pair["timeframe"])
        boundary = _naive(grid.current_candle_start(now - self._window))
        # Бар, ещё не вышедший целиком из окна доказательства, — у той полосы.
        if _naive(grid.bar_close(boundary)) > now - self._window:
            boundary = _naive(grid.previous_candle_start(boundary))
        return min(_naive(horizon), boundary)

    def _level(self, pair):
        obligated = self._worker.execution_boundary(pair["key"][0]) is not None
        if obligated and pair["timeframe"] == "1m":
            return 0
        if obligated:
            return 1
        return 2 if pair["timeframe"] != "1m" else 3

    # ── поиск дыр без сети ──
    def _expected_opens(self, pair, lower, upper, now):
        """Ожидаемые открытия окна: календарь, где применим, иначе сетка ТФ."""
        uid, timeframe = pair["uid"], pair["timeframe"]
        grid = self._timeline.grid(timeframe)
        reconciler = self._worker._reconciler
        calendar = None
        if reconciler is not None and reconciler.market_calendar is not None:
            calendar = reconciler.market_calendar.calendar_for(uid, now.to_pydatetime() if hasattr(now, "to_pydatetime") else now)
        covered, expected = set(), set()
        if calendar is not None:
            from src.market_calendar.core import expected_bars
            from src.market_calendar.intervals import exchange_date
            from src.market_calendar.rules import candle_rule

            covered = {day.trading_date for day in calendar.days}
            rule = candle_rule(timeframe)
            expected = {pd.Timestamp(bar.open) for bar in expected_bars(
                calendar, rule, lower.to_pydatetime(), (upper + pd.Timedelta(microseconds=1)).to_pydatetime(),
                timedelta(0))}
        opens, probe = [], _naive(lower)
        while probe <= upper:
            if calendar is not None and exchange_date(probe.to_pydatetime()) in covered:
                if probe in expected:
                    opens.append(probe)
            else:
                opens.append(probe)
            probe = _naive(grid.bar_close(probe))
        return opens

    def holes(self, pair, now):
        """Дыры окна полноты, требующие проверки сейчас, и момент ближайшей будущей."""
        uid, timeframe = pair["uid"], pair["timeframe"]
        upper = self._upper(pair, now)
        if upper is None:
            return [], None
        lower = self.window_lower(pair, now)
        if lower > upper:
            return [], None
        snapshot = self._store.window_snapshot(source=self._source, instrument_uid=uid, interval=timeframe,
                                               lower=lower, upper=upper)
        due, later = [], None
        for opened in self._expected_opens(pair, lower, upper, now):
            if opened in snapshot["received"]:
                continue
            state = snapshot["states"].get(opened)
            if state is not None and state[0] in RESOLVED_INTERVAL_STATES:
                continue
            check = snapshot["checks"].get(opened)
            if check is not None and check["next_check_at"] is not None and check["next_check_at"] > now:
                later = check["next_check_at"] if later is None else min(later, check["next_check_at"])
                continue
            due.append(opened)
        self.last_holes[uid, timeframe] = len(due)
        absent = sum(1 for state, _ in snapshot["states"].values() if state == "source_absent")
        self._cache.set_lane_status(uid, timeframe, self.name, {
            "window_lower": _text(lower), "window_upper": _text(upper), "holes_due": len(due),
            "first_hole": _text(due[0]) if due else None, "source_absent": absent,
            "next_check_at": None if later is None else _text(later), "checked_at": _text(now)})
        return due, later

    def _first_run(self, pair, due):
        """Первая непрерывная по сетке серия дыр, не длиннее одного чанка."""
        grid = self._timeline.grid(pair["timeframe"])
        span = CHUNK_SPAN[pair["timeframe"]]
        start = end = due[0]
        for opened in due[1:]:
            if opened != _naive(grid.bar_close(end)) or _naive(grid.bar_close(opened)) - start > span:
                break
            end = opened
        return start, end

    def adopt_recovery_books(self):
        """Однократный перенос книг ``RecoveryRange`` прежней очереди (задача 5.5).

        Раунды и парковка книг полосам не нужны: незакрытые минуты книги
        внутри окна полноты — обычные дыры окна, старше окна — не нужны
        потребителям. Книги удаляются одной транзакцией, повторно не читаются.
        """
        self._books_adopted = True
        books = [(uid, timeframe, RecoveryRange.from_json(value))
                 for uid, timeframe, value in self._store.take_live_states(kind="recovery", source=self._source)]
        books = [(uid, timeframe, book) for uid, timeframe, book in books if book is not None]
        for uid, timeframe, book in books:
            self._cache.set_live_gap(uid, timeframe, None)
            log.info("Книга восстановления %s %s %s–%s (%s) передана полосе полноты",
                     uid, timeframe, _text(book.lower), _text(book.upper), book.state)
        if books:
            self.wake()
        return books

    # ── шаг ──
    def step(self, now=None):
        now = _naive(now or self._timeline.now())
        if not self._books_adopted:
            self.adopt_recovery_books()
        pairs = [pair for pair in self._cache.live_pairs()
                 if self._active is None or (pair["uid"], pair["timeframe"]) in self._active]
        by_level: dict[int, list] = {}
        for pair in pairs:
            by_level.setdefault(self._level(pair), []).append(pair)
        for level in sorted(by_level):
            items = by_level[level]
            cursor = self._cursors.get(level, 0)
            for offset in range(len(items)):
                pair = items[(cursor + offset) % len(items)]
                identity = (pair["uid"], pair["timeframe"])
                if now < self._skip_until.get(identity, pd.Timestamp.min):
                    continue
                due, later = self.holes(pair, now)
                if not due:
                    # Дыр к проверке нет: до ближайшей проверки или пересчёта окна.
                    self._skip_until[identity] = min(later, now + self._rescan) if later is not None else now + self._rescan
                    continue
                self._cursors[level] = (cursor + offset + 1) % len(items)
                self._check(pair, *self._first_run(pair, due), now)
                return True
        return False

    def _check(self, pair, start, end, now):
        uid, timeframe = pair["uid"], pair["timeframe"]
        grid = self._timeline.grid(timeframe)
        try:
            self._cache._load(pair["instrument"], timeframe, start_date=start,
                              end_date=_naive(grid.bar_close(end)), wait_for_budget=False, pace=False)
        except RequestDeferred:
            raise
        except Exception as exc:
            # Ошибка источника не засчитывается как пустая проверка.
            self._worker.errors[uid, timeframe] = str(exc)
            self._skip_until[uid, timeframe] = now + timedelta(seconds=60)
            log.warning("Полнота %s %s %s–%s: %s", pair["instrument"].ticker, timeframe, start, end, exc)
            return
        snapshot = self._store.window_snapshot(source=self._source, instrument_uid=uid, interval=timeframe,
                                               lower=start, upper=end)
        missing, probe = [], start
        while probe <= end:
            state = snapshot["states"].get(probe)
            if probe not in snapshot["received"] and not (state is not None and state[0] in RESOLVED_INTERVAL_STATES):
                missing.append(probe)
            probe = _naive(grid.bar_close(probe))
        self._record(uid, timeframe, missing, snapshot["checks"], now)
        if missing:
            self._cache.refresh_live_coverage(uid, timeframe)

    def _record(self, uid, timeframe, missing, checks, now):
        """Пустая проверка: следующий срок либо итог «у источника нет»."""
        groups: dict[object, list] = {}
        final = []
        for opened in missing:
            done = (checks.get(opened) or {}).get("checks", 0) + 1
            if done > len(self._rechecks):
                final.append((opened, done, (checks.get(opened) or {}).get("first_checked_at") or now))
            else:
                groups.setdefault(now + self._rechecks[done - 1], []).append(opened)
        for due, opens in groups.items():
            self._store.record_interval_checks(source=self._source, instrument_uid=uid, interval=timeframe,
                                               open_times=opens, checked_at=now, next_check_at=due)
        if final:
            self._store.record_interval_checks(source=self._source, instrument_uid=uid, interval=timeframe,
                                               open_times=[item[0] for item in final], checked_at=now,
                                               next_check_at=None)
            rows = [(opened, "source_absent",
                     f"source_absent:checks={done}:first={_text(first)}:last={_text(now)}")
                    for opened, done, first in final]
            self._store.set_interval_states(source=self._source, instrument_uid=uid, interval=timeframe,
                                            rows=rows, observed_at=now)
            log.info("Нет у источника: %s %s, интервалов %d после %d проверок",
                     uid, timeframe, len(final), len(self._rechecks) + 1)


class CalendarLane:
    """Привязки инструментов и расписание — на сервисе инструментов."""

    name = "calendar"

    def __init__(self, reconciler, timeline):
        self._reconciler = reconciler
        self._timeline = timeline

    def step(self, now=None):
        if self._reconciler is None:
            return False
        return bool(self._reconciler.service_calendar(_naive(now or self._timeline.now())))


def _text(value):
    return pd.Timestamp(value).isoformat() + "Z"
