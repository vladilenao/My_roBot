"""Службы контура на ленте ядра: спрос — ядру, курсор — после тика (add-contour-market-feed)."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from src.assembly.feed import FeedMarketServices
from src.assembly.services import ContourDemand
from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

NOW = datetime(2026, 10, 12, 12, 3, 30)
NG = Instrument("NG", "NGZ6", "future", "NG-12.26", "uid-ng")


class FakeCore:
    """Ядро в том же процессе: настоящая база и кэш, без сети и полос."""

    def __init__(self, tmp_path):
        self.order = []
        self.timeline = MultiTimeframeScheduler(["1m"], clock=lambda: NOW)
        self.market_store = MarketDataStore(tmp_path / "market.sqlite3")
        self.market_calendar = None
        self.request_budget = RequestBudget(lambda: NOW, 0)
        self.data_cache = MarketDataCache(lambda **kw: (pd.DataFrame(), kw["instrument_id"]), self.timeline,
                                          request_budget=self.request_budget, market_store=self.market_store)
        self.tracks_obligations = True
        self.obligations = []

    def serve(self, demand):
        self.order.append("serve")
        self.demand = demand
        self.data_cache.register_live_pair(NG, NG.uid, "1m")
        self.data_cache.mark_live_history(NG.uid, "1m", done=True)
        self.market_store.record_candles(source="tbank_exchange", instrument_uid=NG.uid, interval="1m",
                                         candles=pd.DataFrame({"datetime": [pd.Timestamp("2026-10-12 12:00")],
                                                               "open": [3.1], "high": [3.2], "low": [3.0],
                                                               "close": [3.15], "volume": [5], "is_complete": [True]}))
        return lambda: self.order.append("core-wait") or True

    def update_obligations(self, obligations):
        self.obligations.append(obligations)

    def cluster_gate(self, state_dir):
        return "gate"

    def close(self):
        self.order.append("core.close")


def test_serve_waits_for_core_then_feed_and_acknowledges_after_tick(tmp_path, monkeypatch):
    monkeypatch.setattr("src.assembly.feed.MARKET_DATA_SETTINGS", SimpleNamespace(
        calendar_evidence=True, candle_publication_timeout_secs=65, candle_publication_timeout_by_tf={},
        startup_wait_timeout_secs=5))
    core = FakeCore(tmp_path)
    services = FeedMarketServices(core, contour_id="emulation", poll_seconds=0.05)
    assert services.data_cache.contour_id == "emulation" and services.cluster_gate(tmp_path) == "gate"
    try:
        waiter = services.serve(ContourDemand((NG,), history_requests=((NG, "1m", 1),)))
        assert core.demand.instruments == (NG,)
        assert waiter() is True and core.order == ["serve", "core-wait"]
        frame = services.data_cache.frame_for(NG, "1m")
        assert list(frame["close"]) == [3.15]
        assert services.feed_lag() == 0
        services.update_obligations("obligations")
        assert core.obligations == ["obligations"] and services.tracks_obligations
        services.acknowledge_tick()
        saved = core.market_store.checkpoint("contour-emulation")
        assert saved["acknowledged_through"] == core.market_store.source_info().max_change_id
    finally:
        services.close()
    assert core.order[-1] == "core.close" and not services.reader._thread.is_alive()


def test_acknowledge_before_serve_does_nothing(tmp_path):
    core = FakeCore(tmp_path)
    services = FeedMarketServices(core, contour_id="emulation")
    services.acknowledge_tick()
    assert core.market_store.checkpoint("contour-emulation") is None
