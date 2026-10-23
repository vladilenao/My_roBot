"""Рыночные службы контура на ленте ядра (add-contour-market-feed, П3.1).

Контур получает свечи не из памяти ядра, а из рыночной базы: свой кэш
(``FollowerMarketDataCache``) собирается снимком и дополняется лентой
``changes`` с курсора ``contour-<вид>``. Ядро по-прежнему в том же процессе:
потребности контура и запросы истории передаются ему вызовом. В П3.2 этот
вызов заменит межпроцессный канал, а остальное останется прежним.

Модуль не импортирует модули ядра: ядро передаётся объектом с теми же
методами порта ``MarketServices``.
"""

from __future__ import annotations

from src.assembly.services import ContourDemand
from src.config import CATCH_UP_BARS, MARKET_DATA_SETTINGS
from src.data.follower import FeedReader, FollowerMarketDataCache
from src.data.market_store import CONTOUR_CONSUMER_PREFIX, SOURCE_TBANK
from src.data.reconciliation import MarketDataReconciler
from src.logging_setup import get_logger

log = get_logger(__name__)


class _CoreLink:
    """Потребности кэша контура, которые прежде ядро видело в своей памяти."""

    def __init__(self, core_cache) -> None:
        self._cache = core_cache

    def request_history(self, instrument, timeframe, min_bars):
        self._cache.request_live_history(instrument, timeframe, min_bars)

    def touch(self, instrument, timeframe):
        self._cache.frame_for(instrument, timeframe)


class FeedMarketServices:
    """``MarketServices`` контура: кэш из ленты ядра, отметки под ключом контура."""

    def __init__(self, core, *, contour_id: str, poll_seconds: float = 1.0) -> None:
        self._core = core
        self.timeline = core.timeline
        self.market_store = core.market_store
        self.market_calendar = core.market_calendar
        self.request_budget = core.request_budget
        settings = MARKET_DATA_SETTINGS
        reconciler = MarketDataReconciler(
            core.market_store, source=SOURCE_TBANK, market_calendar=core.market_calendar,
            calendar_evidence=settings.calendar_evidence,
            candle_publication_timeout_secs=settings.candle_publication_timeout_secs,
            candle_publication_timeout_by_tf=settings.candle_publication_timeout_by_tf,
            read_only=True,
        )
        self.data_cache = FollowerMarketDataCache(
            core.timeline, core.market_store, core=_CoreLink(core.data_cache),
            market_reconciler=reconciler, market_calendar=core.market_calendar,
            request_budget=core.request_budget, freshness_tolerance_bars=CATCH_UP_BARS,
        )
        self.data_cache.contour_id = contour_id
        self.data_cache.feed_lag = self.feed_lag
        self.reader = FeedReader(self.data_cache, core.market_store,
                                 consumer_id=CONTOUR_CONSUMER_PREFIX + contour_id, poll_seconds=poll_seconds)
        self._started = False

    @property
    def tracks_obligations(self) -> bool:
        return self._core.tracks_obligations

    def cluster_gate(self, state_dir):
        # Кластерный поток и вердикт пока у ядра, как в П2.
        return self._core.cluster_gate(state_dir)

    def serve(self, demand: ContourDemand):
        """Спрос — ядру, пары спроса — читателю ленты; ожидание старта ждёт и ядро, и ленту."""
        for instrument, timeframe, count in demand.history_requests:
            self.data_cache.want(instrument, timeframe, count)
        core_waiter = self._core.serve(demand)
        self.reader.start()
        self._started = True
        timeout = MARKET_DATA_SETTINGS.startup_wait_timeout_secs

        def wait():
            ready = core_waiter() if core_waiter is not None else True
            # Ядро прогрело пары — кэш контура догоняет ленту до текущего края.
            target = self.market_store.source_info().max_change_id
            caught = self.reader.wait_applied(target, timeout) and self.reader.wait_cycles(2, timeout)
            if not caught:
                log.warning("Кэш контура не догнал ленту ядра за %d с (применено %d из %d)",
                            timeout, self.reader.applied, target)
            return ready and caught
        return wait

    def update_obligations(self, obligations) -> None:
        self._core.update_obligations(obligations)

    def acknowledge_tick(self) -> None:
        if self._started:
            self.reader.acknowledge()

    def feed_lag(self) -> int:
        """Сколько изменений ленты кэш контура ещё не применил (для сердцебиения)."""
        return max(0, self.market_store.source_info().max_change_id - self.reader.applied)

    def close(self) -> None:
        """Сначала читатель ленты, затем ядро в прежнем порядке."""
        self.reader.stop()
        self._core.close()
