"""Кэш свечей торгового контура: только чтение рыночной базы ядра (add-contour-market-feed).

Контур больше не читает память ядра. Его кэш собирается из снимка рыночной
базы и дополняется лентой ``changes`` с собственного курсора, а готовность пар
берёт из опубликованной ядром таблицы ``pair_readiness``. Сеть за свечами кэш
не вызывает: недостающая история передаётся ядру через ``CoreLink``.

Всё, что торговая сторона спрашивает у кэша (кадры, покрытие, барьер
исполнения, отметки), работает унаследованной логикой ``MarketDataCache``;
переопределены только места, где живой кэш опирался на приём данных ядра.
"""

from __future__ import annotations

import threading
from typing import Protocol

import pandas as pd

from src.data.cache import MarketDataCache, _naive
from src.data.market_store import SOURCE_TBANK, CursorExpired, ProducerMismatch
from src.logging_setup import get_logger

log = get_logger(__name__)

# Порция чтения ленты: короткие транзакции, файл -wal не растёт.
FEED_PAGE = 500
_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "is_complete"]


class CoreLink(Protocol):
    """Связь кэша контура с ядром: потребности, которые прежде видела память ядра."""

    def request_history(self, instrument, timeframe: str, min_bars: int) -> None: ...

    def touch(self, instrument, timeframe: str) -> None: ...


def _no_network(**_):
    raise RuntimeError("Кэш торгового контура не загружает свечи из сети")


class FollowerMarketDataCache(MarketDataCache):
    """Кэш контура: кадры из снимка и ленты, готовность пар — из базы ядра."""

    def __init__(self, timeline, market_store, *, core: CoreLink, market_reconciler=None, market_calendar=None,
                 request_budget=None, market_source=SOURCE_TBANK, freshness_tolerance_bars=0) -> None:
        super().__init__(_no_network, timeline, freshness_tolerance_bars=freshness_tolerance_bars,
                         market_store=market_store, market_source=market_source,
                         market_reconciler=market_reconciler, market_calendar=market_calendar)
        # Лимиты ядра — только для диагностики «ожидание сброса лимита».
        self._request_budget = request_budget
        self.live_session_enabled = True
        self._owned_live_ingestion = True
        self._live_cutover = None
        self._core = core
        self._wanted: dict[tuple, tuple] = {}
        self._floors: dict[tuple, int] = {}
        self._absent: set[tuple] = set()

    # ── Потребности торговой стороны: передаются ядру, кадр — из памяти ──

    def _want(self, key, instrument, timeframe, min_bars):
        previous = self._wanted.get(key, (instrument, timeframe, 0))[2]
        self._wanted[key] = (instrument, timeframe, max(previous, min_bars))
        self._instruments.setdefault(key, instrument)

    def _bound(self, key, timeframe):
        uid = self._uids.get(key)
        return uid is not None and (uid, timeframe) in self._live_members

    def frame_for(self, instrument, timeframe: str) -> pd.DataFrame:
        with self._lock:
            key = self._key(instrument, timeframe)
            unknown = not self._bound(key, timeframe)
            if unknown:
                self._want(key, instrument, timeframe, 1)
            frame = self._frames.get(key)
            result = pd.DataFrame() if frame is None or frame.empty else self._closed_only(frame, timeframe)
        if unknown:
            # Как чтение кадра неизвестной пары у ядра: потребность прогрева.
            self._core.touch(instrument, timeframe)
        return result

    def request_live_history(self, instrument, timeframe, min_bars):
        with self._lock:
            key = self._key(instrument, timeframe)
            self._want(key, instrument, timeframe, min_bars)
            frame = self._frames.get(key)
            result = pd.DataFrame() if frame is None or frame.empty else self._closed_only(frame, timeframe)
        self._core.request_history(instrument, timeframe, min_bars)
        return result

    def want(self, instrument, timeframe, min_bars=1):
        """Пара потребностей контура, которую ядро обслуживает по ``ContourDemand``."""
        with self._lock:
            self._want(self._key(instrument, timeframe), instrument, timeframe, min_bars)

    # ── Готовность пар: опубликована ядром ──

    def live_floor(self, uid, timeframe):
        member = self._live_members.get((uid, timeframe))
        floor = None if member is None else member.get("live_floor")
        return None if floor is None else _naive(pd.Timestamp(floor))

    def _refresh_member_state(self, uid, timeframe, member):
        held = self._market_store.live_state(kind="uid", source=self._market_source, instrument_uid=uid)
        return "INTEGRITY_HOLD" if held.get("integrity_hold") else member["state"]

    def _publish_readiness(self, uid, timeframe, member, *, lanes_only=False):
        return None

    def pair_status(self, instrument, timeframe):
        with self._lock:
            uid = self._uids.get(self._key(instrument, timeframe))
            member = self._live_members.get((uid, timeframe)) if uid else None
            if member is None:
                return {"state": "BOOTSTRAPPING", "uid": uid, "reason": "Пара ещё не зарегистрирована ingestion"}
            state = self._refresh_member_state(uid, timeframe, member)
            cursors = self._market_store.cursors(source=self._market_source, instrument_uid=uid, interval=timeframe,
                                                 contour=self.contour_id)
            held = self._market_store.live_state(kind="uid", source=self._market_source, instrument_uid=uid)
            return {"state": state, "uid": uid, "transport": member.get("transport") or "polling",
                    "epoch": member.get("epoch"), "history": member["history"],
                    "history_error": member["history_error"], "gap": member.get("gap"),
                    "received_through": cursors.get("received_through"),
                    "resolved_through": cursors.get("resolved_through"),
                    "processed_through": cursors.get("processed_through"),
                    "incident": held.get("incident"),
                    "cutover": member.get("cutover"),
                    "pre_cutover_gaps": self._pre_cutover_gaps.get((uid, timeframe), 0),
                    "lanes": {lane: dict(value) for lane, value in (member.get("lanes") or {}).items()}}

    def apply_readiness(self, rows) -> list[tuple]:
        """Обновляет пары по готовности ядра; возвращает новые привязки ``(ключ, UID, ТФ)``.

        Пара, которую ядро сняло с обслуживания, снимается и здесь. Новую
        привязку кадром заполняет ридер ленты (``bind``).
        """
        index = {(row["instrument_uid"], row["interval"]): row for row in rows}
        changed = False
        with self._lock:
            for pair in list(self._live_members):
                row = index.get(pair)
                if row is None:
                    self._unbind(pair)
                    changed = True
                    continue
                changed |= self._update_member(self._live_members[pair], row)
            fresh = []
            for key, (instrument, timeframe, _) in sorted(self._wanted.items()):
                if self._bound(key, timeframe):
                    continue
                uid = self._resolve(instrument, timeframe, index)
                if uid is not None:
                    fresh.append((key, uid, timeframe))
            if changed:
                self._notify()
        return fresh

    @staticmethod
    def _resolve(instrument, timeframe, index):
        uid = getattr(instrument, "uid", None)
        if uid:
            return uid if (uid, timeframe) in index else None
        found = {pair_uid for (pair_uid, tf), row in index.items() if tf == timeframe
                 and row.get("ticker") == instrument.ticker and row.get("instrument_type") == instrument.instrument_type}
        return found.pop() if len(found) == 1 else None

    @staticmethod
    def _update_member(member, row):
        values = {name: row.get(name) for name in ("state", "transport", "epoch", "history", "gap",
                                                    "cutover", "live_floor")}
        values["history_error"] = row.get("history_error") or ""
        values["lanes"] = row.get("lanes") or {}
        changed = any(member.get(name) != value for name, value in values.items() if name != "lanes")
        member.update(values)
        return changed

    def bind(self, key, uid, timeframe, frame, *, floor, absent=(), row=None):
        """Новая пара контура: кадр из снимка; лента до ``floor`` уже в нём."""
        with self._lock:
            instrument = self._wanted.get(key, (self._instruments.get(key),))[0]
            self._uids[key] = uid
            self._instruments[key] = instrument
            member = self._live_members.setdefault((uid, timeframe), {
                "keys": set(), "instrument": instrument, "min_bars": 0, "execution_lower": None,
                "history": "pending", "warmup_days": 0, "history_error": "", "state": "BOOTSTRAPPING"})
            member["keys"].add(key)
            if row is not None:
                self._update_member(member, row)
            frame = frame if frame is not None and not frame.empty else pd.DataFrame(columns=_COLUMNS)
            self._frames[key] = frame.reset_index(drop=True)
            closed = self._closed_only(frame, timeframe) if not frame.empty else frame
            if not closed.empty:
                self._last_loaded[key] = _naive(closed["datetime"].max())
            self._floors[uid, timeframe] = max(floor, self._floors.get((uid, timeframe), floor))
            self._absent.update((uid, _naive(stamp)) for stamp in absent)
            self._notify()

    def _unbind(self, pair):
        member = self._live_members.pop(pair, None)
        if member is None:
            return
        for key in member["keys"]:
            self._uids.pop(key, None)
            self._frames.pop(key, None)
            self._last_loaded.pop(key, None)
        self._floors.pop(pair, None)

    def unbind_all(self):
        """Пересборка после устаревшего курсора: пары заново привязываются снимком."""
        with self._lock:
            for pair in list(self._live_members):
                self._unbind(pair)
            self._absent.clear()
            self._notify()

    # ── Лента изменений ──

    def apply_changes(self, changes) -> int:
        """Свечи и состояния интервалов пар контура; остальное пропускается."""
        incoming: dict[tuple, list] = {}
        applied = 0
        with self._lock:
            for change in changes:
                kind, change_id = change["kind"], change["change_id"]
                body = change.get("candle") or change.get("interval_status") or {}
                pair = body.get("instrument_uid"), body.get("interval")
                if pair not in self._live_members or change_id <= self._floors.get(pair, 0):
                    continue
                applied += 1
                stamp = _naive(pd.Timestamp(body["open_time_utc"]))
                if kind == "interval_status":
                    if pair[1] == "1m" and str(body.get("evidence", "")).startswith("source_absent"):
                        self._absent.add((pair[0], stamp))
                    continue
                if not body.get("is_complete"):
                    continue
                row = {"datetime": stamp, **{name: float(body[name]) for name in ("open", "high", "low", "close")},
                       "volume": int(body["volume"]), "is_complete": True}
                incoming.setdefault(pair, []).append(row)
                if pair[1] == "1m" and (pair[0], stamp) in self._absent:
                    self._absent.discard((pair[0], stamp))
                    self._late_after_source_absent.append((pair[0], stamp, tuple(
                        row[name] for name in ("open", "high", "low", "close"))))
            for pair, rows in incoming.items():
                member = self._live_members[pair]
                frame_in = pd.DataFrame(rows, columns=_COLUMNS)
                for key in member["keys"]:
                    frame = self._frames.get(key)
                    self._frames[key] = (frame_in.sort_values("datetime").drop_duplicates("datetime", keep="last")
                                         .reset_index(drop=True) if frame is None or frame.empty
                                         else self._merge_new_bars(frame, frame_in, self._last_loaded.get(key)))
                    self._last_loaded[key] = max(frame_in["datetime"].max(), self._last_loaded.get(key, pd.Timestamp.min))
            if applied:
                self._notify()
        return applied

    def _notify(self):
        self._live_generation += 1
        self._data_changed.notify_all()

    def bound_keys(self):
        with self._lock:
            return {key for key, (_, timeframe, _) in self._wanted.items() if self._bound(key, timeframe)}

    def wanted_keys(self):
        with self._lock:
            return set(self._wanted)


class FeedReader:
    """Фоновый читатель ленты ядра для кэша контура.

    Цикл: готовность пар → снимок новых пар → ``changes_after`` порциями до
    конца ленты. Устаревший курсор или смена производителя — пересборка из
    снимка с предупреждением; пропущенное отдельно не проигрывается.
    """

    def __init__(self, cache: FollowerMarketDataCache, store, *, consumer_id: str, poll_seconds: float = 1.0,
                 source: str = SOURCE_TBANK) -> None:
        self._cache = cache
        self._store = store
        self._source = source
        self.consumer_id = consumer_id
        self._poll = poll_seconds
        self._producer = None
        self._cursor = 0
        self._applied = 0
        self._cycles = 0
        self._caught = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="contour-feed", daemon=True)

    @property
    def producer_id(self):
        return self._producer

    @property
    def applied(self) -> int:
        with self._caught:
            return self._applied

    def start(self) -> None:
        self._open()
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _open(self) -> None:
        """Курсор до снимка: подтверждённый, если он ещё в ленте, иначе текущий край."""
        info = self._store.source_info()
        saved = self._store.checkpoint(self.consumer_id)
        self._producer = info.producer_id
        if saved is not None and saved["producer_id"] == info.producer_id and saved["acknowledged_through"] >= info.min_after_id:
            self._cursor = min(saved["acknowledged_through"], info.max_change_id)
        else:
            if saved is not None:
                log.warning("Лента ядра: курсор %s устарел — кэш контура собирается из снимка", self.consumer_id)
            self._cursor = info.max_change_id
        with self._caught:
            self._applied = self._cursor

    def _rebuild(self, reason: str) -> None:
        log.warning("Лента ядра: %s — кэш контура пересобирается из снимка", reason)
        self._cache.unbind_all()
        info = self._store.source_info()
        self._producer, self._cursor = info.producer_id, info.max_change_id

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Лента ядра: ошибка чтения")
            self._stop.wait(self._poll)

    def poll_once(self) -> None:
        rows = self._store.pair_readiness_all(source=self._source)
        index = {(row["instrument_uid"], row["interval"]): row for row in rows}
        for key, uid, timeframe in self._cache.apply_readiness(rows):
            # Край ленты — до снимка: всё до него уже в кадре.
            floor = self._store.source_info().max_change_id
            frame = self._store.current_frame(source=self._source, instrument_uid=uid, interval=timeframe)
            absent = (self._store.source_absent_opens(source=self._source, instrument_uid=uid, interval=timeframe)
                      if timeframe == "1m" else ())
            self._cache.bind(key, uid, timeframe, frame, floor=floor, absent=absent, row=index[uid, timeframe])
        while not self._stop.is_set():
            try:
                page = self._store.changes_after(self._producer, self._cursor, FEED_PAGE)
            except CursorExpired:
                self._rebuild("курсор позади удалённого края")
                return
            except ProducerMismatch:
                self._rebuild("сменился производитель рыночной базы")
                return
            self._cache.apply_changes(page["changes"])
            self._cursor = page["next_after_id"]
            if not page["has_more"]:
                break
        with self._caught:
            self._applied = self._cursor
            self._cycles += 1
            self._caught.notify_all()

    def wait_cycles(self, count: int, timeout: float) -> bool:
        """Ждёт ``count`` полных циклов, начатых после вызова: новые пары успели привязаться."""
        with self._caught:
            target = self._cycles + count
            return self._caught.wait_for(lambda: self._cycles >= target, timeout=timeout)

    def wait_applied(self, target: int, timeout: float) -> bool:
        with self._caught:
            return self._caught.wait_for(lambda: self._applied >= target, timeout=timeout)

    def acknowledge(self) -> None:
        """Подтверждение применённого края после commit тика контура."""
        applied = self.applied
        if self._producer is None or applied <= 0:
            return
        try:
            self._store.acknowledge(self.consumer_id, self._producer, applied)
        except (ProducerMismatch, ValueError) as exc:
            log.warning("Лента ядра: курсор %s не подтверждён (%s)", self.consumer_id, type(exc).__name__)
