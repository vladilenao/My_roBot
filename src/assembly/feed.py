"""Рыночные службы процесса контура на ленте ядра (add-contour-market-feed, add-contour-process).

Контур живёт отдельным процессом и не держит ни одного объекта ядра. Свечи —
свой кэш (``FollowerMarketDataCache``) из снимка рыночной базы и ленты
``changes`` с курсора ``contour-<вид>``. Потребности — строками в рыночной базе
под ``contour_id``: спрос пар и глубины истории (``contour_demand``),
обязательства незавершённых сделок (``contour_obligations``); ядро их
опрашивает. Календарь — расписание из базы без сети, арбитра квот нет:
контур к брокеру не обращается.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from src.assembly.contracts import instrument_payload
from src.assembly.services import ContourDemand, _timeframes
from src.config import CATCH_UP_BARS, CLUSTERS_ENABLED, MARKET_DATA_SETTINGS, SLEEP_SECONDS
from src.data.follower import FeedReader, FollowerMarketDataCache
from src.data.market_store import CONTOUR_CONSUMER_PREFIX, SOURCE_TBANK
from src.data.reconciliation import MarketDataReconciler
from src.instruments.model import instrument_key
from src.logging_setup import get_logger

log = get_logger(__name__)


class StoreCoreLink:
    """Потребности кэша контура для ядра — строки ``contour_demand`` под ключом контура."""

    def __init__(self, market_store, contour_id: str) -> None:
        self._store = market_store
        self._contour_id = contour_id
        self._sent: dict[tuple, int] = {}

    def request_history(self, instrument, timeframe, min_bars):
        key = (instrument_key(instrument), timeframe)
        if self._sent.get(key, 0) >= min_bars:
            return
        self._store.record_contour_demand(
            self._contour_id, [(key[0], timeframe, int(min_bars), instrument_payload(instrument))])
        self._sent[key] = int(min_bars)

    def touch(self, instrument, timeframe):
        self.request_history(instrument, timeframe, 1)


class _ClusterReadClock:
    """Момент последнего чтения Redis ядром — из хранилища кластеров (для ``consumer_lag``)."""

    def __init__(self, store) -> None:
        self._store = store

    @property
    def last_read_at(self) -> datetime | None:
        from src.clusters.store import parse_utc

        value = self._store.state("last_read_at")
        return None if value is None else parse_utc(value)


class FeedMarketServices:
    """``MarketServices`` процесса контура: кэш из ленты, потребности строками в базе."""

    def __init__(self, market_store, *, contour_id: str, data_dir: Path, tracks_obligations: bool,
                 poll_seconds: float = 1.0, timeline=None, market_calendar=None) -> None:
        from src.market_calendar.service import MarketCalendar, StoredScheduleSource
        from src.scheduler.timing import MultiTimeframeScheduler

        settings = MARKET_DATA_SETTINGS
        self.contour_id = contour_id
        self.market_store = market_store
        self.data_dir = Path(data_dir)
        self.request_budget = None
        self.timeline = timeline or MultiTimeframeScheduler(
            timeframes=_timeframes(), sleep_secs=SLEEP_SECONDS, catch_up_bars=CATCH_UP_BARS)
        self.market_calendar = market_calendar or MarketCalendar(
            market_store, StoredScheduleSource(), schedule_max_age_secs=settings.schedule_max_age_secs)
        reconciler = MarketDataReconciler(
            market_store, source=SOURCE_TBANK, market_calendar=self.market_calendar,
            calendar_evidence=settings.calendar_evidence,
            candle_publication_timeout_secs=settings.candle_publication_timeout_secs,
            candle_publication_timeout_by_tf=settings.candle_publication_timeout_by_tf,
            read_only=True,
        )
        self.link = StoreCoreLink(market_store, contour_id)
        self.data_cache = FollowerMarketDataCache(
            self.timeline, market_store, core=self.link,
            market_reconciler=reconciler, market_calendar=self.market_calendar,
            request_budget=None, freshness_tolerance_bars=CATCH_UP_BARS,
        )
        self.data_cache.contour_id = contour_id
        self.data_cache.feed_lag = self.feed_lag
        self.reader = FeedReader(self.data_cache, market_store,
                                 consumer_id=CONTOUR_CONSUMER_PREFIX + contour_id, poll_seconds=poll_seconds)
        self._tracks = tracks_obligations
        self._obligations = None
        self._started = False

    @property
    def tracks_obligations(self) -> bool:
        return self._tracks

    def cluster_gate(self, state_dir):
        """Вердикт по кэшу контура; хранилище кластеров ядра — только чтение (D7)."""
        if not CLUSTERS_ENABLED:
            return None
        from src.clusters.gate import ClusterGate, store_candles
        from src.clusters.store import FILE_NAME, ClusterEventStore
        from src.config import CLUSTER_SETTINGS

        path = self.data_dir / FILE_NAME
        if not path.exists():
            log.warning("Кластеры: нет %s — вердикты будут «нет данных», пока ядро не прочитает поток", path)
        store = ClusterEventStore(path, read_only=True)
        return ClusterGate(store, store_candles(self.market_store, SOURCE_TBANK, self.data_cache.pair_uid),
                           CLUSTER_SETTINGS, consumer=_ClusterReadClock(store), live=True,
                           uid_for=self.data_cache.pair_uid)

    def serve(self, demand: ContourDemand):
        """Спрос — строками ядру, обязательства — снимком; ожидание старта — пары и лента."""
        for instrument, timeframe, count in demand.history_requests:
            self.data_cache.want(instrument, timeframe, count)
            self.link.request_history(instrument, timeframe, count)
        if self._tracks and demand.obligations is not None:
            self.update_obligations(demand.obligations)
        self.reader.start()
        self._started = True
        timeout = MARKET_DATA_SETTINGS.startup_wait_timeout_secs

        def wait():
            deadline = time.monotonic() + timeout
            while self.data_cache.wanted_keys() - self.data_cache.bound_keys() and time.monotonic() < deadline:
                time.sleep(0.2)
            missing = self.data_cache.wanted_keys() - self.data_cache.bound_keys()
            target = self.market_store.source_info().max_change_id
            remaining = max(0.0, deadline - time.monotonic())
            caught = self.reader.wait_applied(target, remaining) and self.reader.wait_cycles(2, max(remaining, 1.0))
            if missing:
                log.warning("Ядро не прогрело %d пар контура за %d с", len(missing), timeout)
            if not caught:
                log.warning("Кэш контура не догнал ленту ядра за %d с (применено %d из %d)",
                            timeout, self.reader.applied, target)
            return not missing and caught
        return wait

    def update_obligations(self, obligations) -> None:
        """Снимок обязательств после тика — одна транзакция, только при изменении."""
        if obligations is None or obligations == self._obligations:
            return
        self.market_store.replace_contour_obligations(self.contour_id, obligations)
        self._obligations = dict(obligations)

    def acknowledge_tick(self) -> None:
        if self._started:
            self.reader.acknowledge()

    def feed_lag(self) -> int:
        """Сколько изменений ленты кэш контура ещё не применил (для сердцебиения)."""
        return max(0, self.market_store.source_info().max_change_id - self.reader.applied)

    def close(self) -> None:
        self.reader.stop()
