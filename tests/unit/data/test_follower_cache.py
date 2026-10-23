"""Кэш контура на ленте ядра (add-contour-market-feed): снимок, лента, готовность из базы."""

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.assembly import contour
from src.data.cache import MarketDataCache
from src.data.follower import FeedReader, FollowerMarketDataCache
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

NOW = datetime(2026, 10, 12, 12, 3, 30)
A = Instrument("AAA", "AAA", "share", "AAA", "aaa")
B = Instrument("BBB", "BBB", "share", "BBB", "bbb")


def bars(start, count, price=100):
    stamps = pd.date_range(pd.Timestamp(start), periods=count, freq="1min")
    return pd.DataFrame({"datetime": stamps, "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1, "is_complete": True})


class Link:
    """Связь с ядром в тесте: запросы контура передаются кэшу ядра и запоминаются."""

    def __init__(self, core):
        self.core, self.requests, self.touched = core, [], []

    def request_history(self, instrument, timeframe, min_bars):
        self.requests.append((instrument.ticker, timeframe, min_bars))
        if self.core is not None:
            self.core.request_live_history(instrument, timeframe, min_bars)

    def touch(self, instrument, timeframe):
        self.touched.append((instrument.ticker, timeframe))
        if self.core is not None:
            self.core.frame_for(instrument, timeframe)


def core_cache(tmp_path):
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: NOW)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), kw["instrument_id"]), timeline,
                            request_budget=RequestBudget(lambda: NOW, 0), market_store=store)
    cache.contour_id = "emulation"
    return timeline, store, cache


def follower(timeline, store, core=None, *, reconciler=True):
    link = Link(core)
    rec = MarketDataReconciler(store, source="tbank_exchange", read_only=True) if reconciler else None
    cache = FollowerMarketDataCache(timeline, store, core=link, market_reconciler=rec,
                                    request_budget=RequestBudget(lambda: NOW, 0))
    cache.contour_id = "emulation"
    reader = FeedReader(cache, store, consumer_id="contour-emulation")
    reader._open()
    return cache, reader, link


def healthy_and_gap(tmp_path):
    """Два UID: у AAA неизвестна 12:01, BBB здоров (как в приёмке reliable-live-candle-ingestion)."""
    timeline, store, cache = core_cache(tmp_path)
    for item in (A, B):
        cache.note_execution_lower(item, pd.Timestamp("2026-10-12 12:00"))
        cache.register_live_pair(item, item.uid, "1m")
        cache.mark_live_history(item.uid, "1m", done=True)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:00", 1).iloc[[0]], received_at=NOW)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:02", 1), received_at=NOW)
    store.set_interval_state(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                             open_time="2026-10-12 12:01", state="pending", evidence="open_session_missing_candle")
    cache.accept_rpc_frame("bbb", "1m", bars("2026-10-12 12:00", 3), received_at=NOW)
    return timeline, store, cache


def executed_minutes(tmp_path, cache):
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    storage = MagicMock()
    storage.load_trades.return_value = ()
    start = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    storage.execution_bar_boundaries.side_effect = lambda: {"aaa": (start, True), "bbb": (start, True)}
    with patch("src.assembly.contour.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch(
        "src.assembly.contour.print_contract_metadata"), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([A, B], cache, MagicMock(), tmp_path / "state")
    assert runtime.post_tick({"1m"}) is True
    executed = {}
    for call in broker.track_bar.call_args_list:
        for uid in call.args[1]:
            executed.setdefault(uid, []).append(call.args[0].minute)
    return executed


def test_decisions_on_feed_match_core_memory(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path / "core")
    expected = executed_minutes(tmp_path / "core", core)

    timeline, store, core = healthy_and_gap(tmp_path / "feed")
    cache, reader, _ = follower(timeline, store, core)
    for item in (A, B):
        cache.want(item, "1m")
    reader.poll_once()
    assert executed_minutes(tmp_path / "feed", cache) == expected == {"aaa": [0], "bbb": [0, 1, 2]}
    assert cache.execution_ready(B, pd.Timestamp("2026-10-12 12:02"))
    assert not cache.execution_ready(A, pd.Timestamp("2026-10-12 12:02"))
    # Отметки контура в общей базе, ядро ничего не ставило под своим именем.
    assert store.cursors(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         contour="emulation")["processed_through"] == "2026-10-12T12:02:00Z"


def test_frame_and_status_match_core(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    pd.testing.assert_frame_equal(cache.frame_for(B, "1m").reset_index(drop=True),
                                  core.frame_for(B, "1m").reset_index(drop=True), check_dtype=False, check_like=True)
    mine, theirs = cache.pair_status(B, "1m"), core.pair_status(B, "1m")
    for name in ("state", "uid", "transport", "history", "history_error", "gap", "received_through",
                 "resolved_through", "cutover", "lanes"):
        assert mine[name] == theirs[name], name
    assert cache.coverage_for(B, "1m", cache.frame_for(B, "1m")) == core.coverage_for(B, "1m", core.frame_for(B, "1m"))


def test_change_between_cursor_and_snapshot_keeps_one_latest_candle(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    floor = store.source_info().max_change_id
    # Исправление пришло после фиксации края, но до чтения снимка.
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:02", 1, price=105))
    frame = store.current_frame(source="tbank_exchange", instrument_uid="bbb", interval="1m")
    cache.bind(cache._key(B, "1m"), "bbb", "1m", frame, floor=floor,
               row=next(row for row in store.pair_readiness_all(source="tbank_exchange") if row["instrument_uid"] == "bbb"))
    reader.poll_once()
    closed = cache.frame_for(B, "1m")
    assert list(closed["datetime"]).count(pd.Timestamp("2026-10-12 12:02")) == 1
    assert closed["close"].iloc[-1] == 105


def test_new_candles_arrive_by_feed_and_raise_generation(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    generation = cache.data_generation
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:03", 1, price=110))
    reader.poll_once()
    assert cache.data_generation > generation
    assert reader.applied == store.source_info().max_change_id


def test_foreign_pairs_are_skipped_but_cursor_passes_them(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    store.record_candles(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                         candles=bars("2026-10-12 12:03", 1))
    reader.poll_once()
    assert cache.frame_for(A, "1m").empty
    assert reader.applied == store.source_info().max_change_id


def test_unknown_pair_goes_to_core_without_network(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, link = follower(timeline, store, core)
    late = Instrument("CCC", "CCC", "share", "CCC", "ccc")
    assert cache.frame_for(late, "1h").empty
    assert cache.prepare_live_history(late, "1h", 50).empty
    reader.poll_once()
    assert link.touched == [("CCC", "1h")] and link.requests == [("CCC", "1h", 50)]
    assert cache.pair_status(late, "1h")["state"] == "BOOTSTRAPPING"
    # Потребность дошла до ядра, сеть контур не трогал (loader запрещает её).
    assert ("CCC", "1h", 50) in [(item.ticker, key[2], count) for key, item, count in core.live_needs()]


def test_pair_without_card_uid_binds_by_ticker(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    selected = Instrument("BBB", "BBB", "share", "BBB")            # live-выбор без UID
    cache.want(selected, "1m")
    reader.poll_once()
    assert cache.pair_uid(selected, "1m") == "bbb"
    assert len(cache.frame_for(selected, "1m")) == 3


def test_cursor_expired_rebuilds_from_snapshot(tmp_path, caplog):
    timeline, store, core = healthy_and_gap(tmp_path)
    store.acknowledge("contour-emulation", store.producer_id, 1)
    store.acknowledge("contour-emulation", store.producer_id, 1)
    with store._connect() as conn:                                   # очистка ушла дальше курсора
        conn.execute("UPDATE metadata SET value='3' WHERE key='min_after_id'")
    with caplog.at_level(logging.WARNING, logger="src.data.follower"):
        cache, reader, _ = follower(timeline, store, core)
    assert any("устарел" in record.getMessage() for record in caplog.records)
    cache.want(B, "1m")
    reader.poll_once()
    assert len(cache.frame_for(B, "1m")) == 3
    assert reader.applied == store.source_info().max_change_id


def test_cursor_expired_while_running_rebuilds(tmp_path, caplog):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    reader._cursor = 0
    with store._connect() as conn:
        conn.execute("UPDATE metadata SET value='2' WHERE key='min_after_id'")
    with caplog.at_level(logging.WARNING, logger="src.data.follower"):
        reader.poll_once()
    assert any("пересобирается" in record.getMessage() for record in caplog.records)
    reader.poll_once()
    assert len(cache.frame_for(B, "1m")) == 3


def test_restart_continues_from_acknowledged_cursor(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    reader.acknowledge()
    acknowledged = reader.applied
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:03", 1))
    again, second, _ = follower(timeline, store, core)
    assert second._cursor == acknowledged
    again.want(B, "1m")
    second.poll_once()
    assert len(again._frames[again._key(B, "1m")]) == 4 and second.applied == store.source_info().max_change_id
    # Очистка ленты держится за курсор контура.
    assert store.checkpoint("contour-emulation")["acknowledged_through"] == acknowledged


def test_pair_removed_by_core_is_unbound(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    core.unregister_live_pair("bbb", "1m")
    reader.poll_once()
    assert cache.pair_status(B, "1m")["state"] == "BOOTSTRAPPING"


def test_late_candle_after_source_absent_is_reported(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(A, "1m")
    reader.poll_once()
    store.set_interval_state(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                             open_time="2026-10-12 12:01", state="source_absent", evidence="source_absent:checks")
    reader.poll_once()
    core.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:01", 1, price=99), received_at=NOW)
    reader.poll_once()
    assert [(uid, stamp) for uid, stamp, _ in cache.drain_late_source_absent()] == [
        ("aaa", pd.Timestamp("2026-10-12 12:01"))]


def test_follower_never_writes_market_state(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    for item in (A, B):
        cache.want(item, "1m")
    reader.poll_once()
    before = store.source_info().max_change_id
    readiness = store.pair_readiness_all(source="tbank_exchange")
    cache.coverage_for(A, "1m", cache.frame_for(A, "1m"))
    cache.pair_status(A, "1m")
    assert store.source_info().max_change_id == before
    assert store.pair_readiness_all(source="tbank_exchange") == readiness
    with pytest.raises(RuntimeError):
        cache._market_reconciler.record_empty_recovery(instrument_uid="aaa", interval="1m", open_time=NOW)


def test_core_publishes_readiness_once_per_state_and_throttles_lanes(tmp_path, monkeypatch):
    timeline, store, core = core_cache(tmp_path)
    writes = []
    original = store.publish_pair_readiness
    monkeypatch.setattr(store, "publish_pair_readiness", lambda **kw: writes.append(kw["status"]["state"]) or original(**kw))
    core.register_live_pair(B, "bbb", "1m")
    core.mark_live_history("bbb", "1m", done=True)
    assert writes == ["BOOTSTRAPPING", "READY"]
    core.pair_status(B, "1m")                                  # чтение не пишет повторно
    core.set_lane_status("bbb", "1m", "evidence", {"checked_at": "12:00"})
    core.set_lane_status("bbb", "1m", "evidence", {"checked_at": "12:01"})
    assert writes == ["BOOTSTRAPPING", "READY"]                # сводка полос — не чаще READINESS_LANES_SECONDS
    row = store.pair_readiness(source="tbank_exchange", instrument_uid="bbb", interval="1m")
    assert row["state"] == "READY" and row["ticker"] == "BBB" and row["instrument_type"] == "share"


def test_core_start_drops_previous_readiness(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.assembly import core as assembly_core

    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    store.publish_pair_readiness(source="tbank_exchange", instrument_uid="bbb", interval="1m", status={"state": "READY"})
    monkeypatch.setattr(assembly_core, "MARKET_DATA_DATABASE_FILE", "market-data.sqlite3")
    provider = SimpleNamespace(calendar_source=lambda *a, **k: None)
    assembly_core._build_market_data(state_dir=tmp_path, clock=None, provider=provider, history=False)
    assert store.pair_readiness_all(source="tbank_exchange") == []
