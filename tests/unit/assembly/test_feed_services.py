"""Службы процесса контура на ленте ядра: спрос и обязательства — строками в базе (add-contour-process)."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from src.assembly.feed import FeedMarketServices, StoreCoreLink
from src.assembly.services import ContourDemand
from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

NOW = datetime(2026, 10, 12, 12, 3, 30)
NG = Instrument("NG", "NGZ6", "future", "NG-12.26", "uid-ng")
SETTINGS = SimpleNamespace(calendar_evidence=True, candle_publication_timeout_secs=65,
                           candle_publication_timeout_by_tf={}, startup_wait_timeout_secs=5,
                           schedule_max_age_secs=129600)


def _core(store):
    """Ядро в тесте: настоящий кэш ядра прогревает пару и публикует готовность в базу."""
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: NOW)
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), kw["instrument_id"]), timeline,
                            request_budget=RequestBudget(lambda: NOW, 0), market_store=store)
    cache.register_live_pair(NG, NG.uid, "1m")
    cache.mark_live_history(NG.uid, "1m", done=True)
    store.record_candles(source="tbank_exchange", instrument_uid=NG.uid, interval="1m",
                         candles=pd.DataFrame({"datetime": [pd.Timestamp("2026-10-12 12:00")], "open": [3.1],
                                               "high": [3.2], "low": [3.0], "close": [3.15], "volume": [5],
                                               "is_complete": [True]}))
    return timeline


def _services(tmp_path, monkeypatch, store, timeline):
    monkeypatch.setattr("src.assembly.feed.MARKET_DATA_SETTINGS", SETTINGS)
    return FeedMarketServices(store, contour_id="emulation", data_dir=tmp_path, tracks_obligations=True,
                              poll_seconds=0.05, timeline=timeline)


def test_serve_writes_demand_waits_for_feed_and_acknowledges_after_tick(tmp_path, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    services = _services(tmp_path, monkeypatch, store, _core(store))
    assert services.data_cache.contour_id == "emulation" and services.request_budget is None
    try:
        waiter = services.serve(ContourDemand((NG,), history_requests=((NG, "1m", 120),)))
        assert [(row["contour_id"], row["instrument_key"], row["interval"], row["min_bars"])
                for row in store.contour_demands()] == [("emulation", "uid-ng", "1m", 120)]
        assert waiter() is True
        assert list(services.data_cache.frame_for(NG, "1m")["close"]) == [3.15]
        assert services.feed_lag() == 0
        services.acknowledge_tick()
        saved = store.checkpoint("contour-emulation")
        assert saved["acknowledged_through"] == store.source_info().max_change_id
    finally:
        services.close()
    assert not services.reader._thread.is_alive()


def test_obligations_snapshot_is_written_only_on_change(tmp_path, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    services = _services(tmp_path, monkeypatch, store, _core(store))
    writes = []
    original = store.replace_contour_obligations
    monkeypatch.setattr(store, "replace_contour_obligations", lambda *a: writes.append(a) or original(*a))
    boundary = {"uid-ng": (datetime(2026, 10, 12, 11, 0), True)}

    services.update_obligations(boundary)
    services.update_obligations(dict(boundary))
    services.update_obligations({})

    assert len(writes) == 2
    assert store.contour_obligations() == {}


def test_acknowledge_before_serve_does_nothing(tmp_path, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    services = _services(tmp_path, monkeypatch, store, _core(store))
    services.acknowledge_tick()
    assert store.checkpoint("contour-emulation") is None


def test_link_grows_demand_and_touch_registers_pair(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    link = StoreCoreLink(store, "emulation")

    link.touch(NG, "15m")
    link.request_history(NG, "15m", 200)
    link.request_history(NG, "15m", 50)

    row, = store.contour_demands()
    assert (row["interval"], row["min_bars"], row["instrument"]["ticker"]) == ("15m", 200, "NGZ6")


def test_cluster_gate_reads_core_store_only(tmp_path, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    services = _services(tmp_path, monkeypatch, store, _core(store))
    monkeypatch.setattr("src.assembly.feed.CLUSTERS_ENABLED", True)

    gate = services.cluster_gate(tmp_path / "contours" / "emulation")

    assert gate is not None and gate._store.read_only
    assert not (tmp_path / "cluster-events.sqlite3").exists()
