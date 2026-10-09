from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from urllib import error, request

import pandas as pd
import pytest

from src.data.export_api import MarketDataExportServer
from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore, ProducerMismatch


def _frame(*, close: float = 101.5, complete: bool = True, stamp: str = "2026-10-09 12:01:00") -> pd.DataFrame:
    return pd.DataFrame([{
        "datetime": pd.Timestamp(stamp), "open": 100.0, "high": 102.0,
        "low": 99.0, "close": close, "volume": 42, "is_complete": complete,
    }])


def _store(tmp_path) -> MarketDataStore:
    return MarketDataStore(tmp_path / "market-data.sqlite3")


def test_store_deduplicates_repeat_and_versions_late_correction(tmp_path):
    store = _store(tmp_path)
    first = store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame())
    again = store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame())
    corrected = store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame(close=101.75))

    assert first == [1]
    assert again == []
    assert corrected == [2]
    changes = store.changes_after(store.producer_id, 0)
    assert [item["candle"]["revision"] for item in changes["changes"]] == [1, 2]
    assert changes["changes"][-1]["candle"]["close"] == "101.75"


def test_preliminary_candle_not_exported_but_completed_version_is(tmp_path):
    store = _store(tmp_path)
    assert store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame(complete=False)) == []
    assert store.changes_after(store.producer_id, 0)["changes"] == []

    changed = store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame(complete=True))
    assert changed == [1]
    assert store.current_frame(source="tbank_exchange", instrument_uid="uid-1", interval="1m").iloc[0]["is_complete"]


def test_checkpoint_is_monotonic_and_bound_to_producer(tmp_path):
    store = _store(tmp_path)
    store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame())
    first = store.acknowledge("historical-emulator", store.producer_id, 1)
    repeat = store.acknowledge("historical-emulator", store.producer_id, 0)

    assert first["acknowledged_through"] == repeat["acknowledged_through"] == 1
    with pytest.raises(ProducerMismatch):
        store.acknowledge("historical-emulator", "another-db", 1)


def test_coverage_status_is_an_exported_change(tmp_path):
    store = _store(tmp_path)
    changed = store.set_interval_state(
        source="tbank_exchange", instrument_uid="uid-1", interval="1m",
        open_time=datetime(2026, 10, 9, 12, 1), state="pending",
        evidence="missing_after_later_candle",
    )

    page = store.changes_after(store.producer_id, 0)
    assert changed == 1
    assert page["changes"][0]["interval_status"]["state"] == "pending"


def test_cache_keeps_a_late_minute_before_already_loaded_newer_bar():
    cache = MarketDataCache.__new__(MarketDataCache)
    current = pd.concat([_frame(stamp="2026-10-09 12:03:00"), _frame(stamp="2026-10-09 12:04:00")], ignore_index=True)
    late = _frame(stamp="2026-10-09 12:01:00")

    merged = cache._merge_new_bars(current, late, pd.Timestamp("2026-10-09 12:04:00"))

    assert list(merged["datetime"]) == [
        pd.Timestamp("2026-10-09 12:01:00"),
        pd.Timestamp("2026-10-09 12:03:00"),
        pd.Timestamp("2026-10-09 12:04:00"),
    ]


def test_store_migrates_early_coverage_table_without_received_state(tmp_path):
    path = tmp_path / "market-data.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE interval_coverage (
                source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                open_time_utc TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('scheduled_closed', 'confirmed_no_trade', 'pending', 'unresolved')),
                evidence TEXT NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY (source, instrument_uid, interval, open_time_utc)
            );
            INSERT INTO interval_coverage VALUES ('tbank_exchange','uid-1','1m','2026-10-09T12:01:00Z','pending','old','2026-10-09T12:02:00Z');
        """)
    store = MarketDataStore(path)
    store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame())

    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-1", interval="1m", open_time="2026-10-09 12:01") == "received"


def _http(base: str, path: str, token: str, data: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    body = None if data is None else json.dumps(data).encode()
    req = request.Request(base + path, data=body, headers=headers, method="PUT" if data is not None else "GET")
    with request.urlopen(req, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_export_api_pages_changes_and_rejects_wrong_token(tmp_path):
    store = _store(tmp_path)
    store.record_candles(source="tbank_exchange", instrument_uid="uid-1", interval="1m", candles=_frame())
    server = MarketDataExportServer(store, "secret", port=0)
    try:
        server.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        _, source = _http(base, "/api/v1/market-data/source", "secret")
        _, page = _http(base, f"/api/v1/market-data/changes?producer_id={source['producer_id']}&after_id=0", "secret")
        _, checkpoint = _http(base, "/api/v1/market-data/consumers/historical-emulator/checkpoint", "secret", {"producer_id": source["producer_id"], "change_id": page["next_after_id"]})

        assert page["changes"][0]["kind"] == "candle_upsert"
        assert checkpoint["acknowledged_through"] == 1
        with pytest.raises(error.HTTPError) as exc:
            _http(base, "/api/v1/market-data/source", "wrong")
        assert exc.value.code == 401
    finally:
        server.close()
