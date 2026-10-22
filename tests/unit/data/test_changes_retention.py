"""Хранение ленты /changes рыночной базы (add-changes-retention)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.data.ingestion_settings import MarketDataSettings, SettingError
from src.data.lanes import ChangesRetentionLane
from src.data.market_store import ChangesPrune, CursorExpired, MarketDataStore

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=7)
OLD = "2026-10-01T00:00:00Z"
FRESH = "2026-10-21T00:00:00Z"


def _candle(minute: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "datetime": pd.Timestamp("2026-10-09 12:00:00") + pd.Timedelta(minutes=minute),
        "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1, "is_complete": True,
    }])


def _store(tmp_path, count: int = 5) -> MarketDataStore:
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    for minute in range(count):
        store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_candle(minute))
    _age(store, OLD)
    return store


def _age(store: MarketDataStore, recorded_at: str, *, first: int = 0) -> None:
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE changes SET recorded_at=? WHERE change_id>?", (recorded_at, first))


def _ids(store: MarketDataStore) -> list[int]:
    with sqlite3.connect(store.path) as conn:
        return [row[0] for row in conn.execute("SELECT change_id FROM changes ORDER BY change_id")]


def test_old_feed_without_consumers_is_pruned_and_cursor_floor_rises(tmp_path):
    store = _store(tmp_path)

    result = store.prune_changes(CUTOFF)

    assert result == ChangesPrune(deleted=5, min_after_id=5, more=False)
    assert _ids(store) == []
    info = store.source_info()
    assert (info.min_after_id, info.max_change_id) == (5, 5)
    with pytest.raises(CursorExpired):
        store.changes_after(info.producer_id, 4)


def test_fully_pruned_feed_still_serves_and_acknowledges_floor_cursor(tmp_path):
    store = _store(tmp_path)
    store.prune_changes(CUTOFF)
    producer = store.producer_id

    page = store.changes_after(producer, 5)
    ack = store.acknowledge("historical-emulator", producer, 5)

    assert page["changes"] == [] and page["next_after_id"] == 5
    assert ack["acknowledged_through"] == 5
    store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_candle(10))
    assert [item["change_id"] for item in store.changes_after(producer, 5)["changes"]] == [6]


def test_prune_removes_only_prefix_and_stops_at_fresh_change(tmp_path):
    store = _store(tmp_path)
    _age(store, FRESH, first=3)

    result = store.prune_changes(CUTOFF)

    assert (result.deleted, result.min_after_id, result.more) == (3, 3, False)
    assert _ids(store) == [4, 5]


def test_unacknowledged_changes_are_kept_and_holder_reported(tmp_path):
    store = _store(tmp_path)
    store.acknowledge("historical-emulator", store.producer_id, 2)

    result = store.prune_changes(CUTOFF)

    assert result == ChangesPrune(deleted=2, min_after_id=2, more=False, holder="historical-emulator", holder_lag=3)
    assert _ids(store) == [3, 4, 5]
    assert store.prune_changes(CUTOFF).deleted == 0


def test_slowest_consumer_holds_the_feed(tmp_path):
    store = _store(tmp_path)
    store.acknowledge("fast", store.producer_id, 5)
    store.acknowledge("slow", store.producer_id, 1)

    result = store.prune_changes(CUTOFF)

    assert (result.deleted, result.holder, result.holder_lag) == (1, "slow", 4)


def test_schedule_checkpoint_does_not_hold_candle_feed(tmp_path):
    store = _store(tmp_path)
    store.record_schedule_binding(source="tbank", instrument_uid="uid-1", exchange="MOEX")
    store.acknowledge_schedule("historical-emulator", store.producer_id, 0)

    result = store.prune_changes(CUTOFF)

    assert (result.deleted, result.holder) == (5, None)
    assert store.schedule_changes_after(store.producer_id, 0)["changes"]


def test_checkpoint_of_other_producer_is_ignored(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO consumer_checkpoints VALUES ('old-db', 'other-producer', 0, ?)", (OLD,))

    assert store.prune_changes(CUTOFF).deleted == 5


def test_prune_goes_in_batches(tmp_path):
    store = _store(tmp_path)

    results = [store.prune_changes(CUTOFF, limit=2) for _ in range(3)]

    assert [(item.deleted, item.min_after_id, item.more) for item in results] == [(2, 2, True), (2, 4, True), (1, 5, False)]


class _Store:
    def __init__(self, *results):
        self.results = list(results)
        self.cutoffs = []

    def prune_changes(self, older_than):
        self.cutoffs.append(older_than)
        return self.results.pop(0)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args)


def test_lane_runs_pass_in_batches_then_waits_interval(monkeypatch):
    log = _Log()
    monkeypatch.setattr("src.data.lanes.log", log)
    store = _Store(ChangesPrune(5000, 5000, True), ChangesPrune(10, 5010, False, "historical-emulator", 7),
                   ChangesPrune(0, 5010, False))
    lane = ChangesRetentionLane(store, 7)

    assert lane.step(NOW) is False
    assert lane.step(NOW + timedelta(seconds=1)) is False
    assert lane.step(NOW + timedelta(minutes=30)) is False
    assert lane.step(NOW + timedelta(minutes=61)) is False

    assert store.cutoffs == [NOW - timedelta(days=7), NOW + timedelta(seconds=1) - timedelta(days=7),
                             NOW + timedelta(minutes=61) - timedelta(days=7)]
    assert log.lines == [
        "Лента изменений рыночной базы: удалено 5010 записей старше 7 сут, минимальный курсор 5010",
        "Лента изменений рыночной базы: очистку удерживает потребитель historical-emulator, не дочитано 7 записей",
    ]


def test_retention_setting_default_and_bounds():
    assert MarketDataSettings().changes_retention_days == 7
    assert MarketDataSettings(changes_retention_days=0).changes_retention_days == 0
    for bad in (-1, 1.5, True):
        with pytest.raises(SettingError):
            MarketDataSettings(changes_retention_days=bad)
