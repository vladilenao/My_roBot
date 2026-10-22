"""Рыночные службы исторического прогона: эмулятор, виртуальные часы, без приёма и API."""

from __future__ import annotations

from src.assembly import core as _core
from src.assembly.services import ContourDemand
from src.config import CLUSTER_SETTINGS, CLUSTERS_ENABLED, runtime_dir


class HistoricalMarketServices:
    """Реализация ``MarketServices`` для прогона истории.

    Данные собираются один раз на прогон по виртуальным часам; полосы, приём,
    поток, локальный API и обработчик паттернов не запускаются.
    """

    pattern_sink = None
    tracks_obligations = False

    def __init__(self, state_dir, clock, client_provider, market_calendar=None, boundary_checks=()) -> None:
        self.state_dir = state_dir
        (self.timeline, self.market_store, self.request_budget, self.market_calendar,
         self.market_reconciler, self.data_cache) = _core._build_market_data(
            state_dir=state_dir, clock=clock, provider=client_provider, history=True,
            market_calendar=market_calendar)
        self.data_cache.set_boundary_checks(boundary_checks)

    def cluster_gate(self, state_dir):
        """Своё хранилище в каталоге прогона: копия боевого (если есть) плюс разовая догрузка потока."""
        if not CLUSTERS_ENABLED:
            return None
        from src.clusters.consumer import catch_up
        from src.clusters.gate import ClusterGate, cache_candles
        from src.clusters.store import FILE_NAME, ClusterEventStore, copy_store

        copy_store(runtime_dir() / FILE_NAME, state_dir / FILE_NAME)
        store = ClusterEventStore(state_dir / FILE_NAME)
        catch_up(store, _core._cluster_redis(), stream=CLUSTER_SETTINGS.stream)
        return ClusterGate(store, cache_candles(self.data_cache), CLUSTER_SETTINGS, uid_for=self.data_cache.pair_uid)

    def serve(self, demand: ContourDemand):
        return None

    def update_obligations(self, obligations) -> None:
        return None

    def close(self) -> None:
        return None
