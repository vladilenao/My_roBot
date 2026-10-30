"""Отметки исполнения под ключом контура и готовность пар (add-contour-market-feed)."""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from urllib import error, request

import pandas as pd
import pytest

from src.data.export_api import MarketDataExportServer
from src.data.live_state import normalize_rpc_candle
from src.data.market_store import MarketDataStore

IDENTITY = dict(source="tbank_exchange", instrument_uid="u", interval="1m")
OPEN = datetime(2026, 10, 12, 12)


def observation(price=100, *, transport="rpc", minute=0):
    stamp = OPEN + pd.Timedelta(minutes=minute)
    row = dict(datetime=stamp, open=price, high=price + 1, low=price - 1, close=price, volume=1, is_complete=True)
    received = (stamp + pd.Timedelta(minutes=1, seconds=2)).isoformat() + "Z"
    result = normalize_rpc_candle(row, uid="u", timeframe="1m", received_at=received, actual_source=1)
    return replace(result, transport=transport)


def held(store):
    return store.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u")


def _legacy(path):
    """База до изменения: границы в market_cursors, курсоры анализа без контура, input_reference."""
    store = MarketDataStore(path)
    store.accept_observation(observation(100))
    store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    with sqlite3.connect(path) as conn:
        key = store._live_key("candle", IDENTITY["source"], "u", "1m", OPEN)
        facts = json.loads(conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0])
        facts["input_reference"] = facts.pop("input_references")["emulation"]
        conn.execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(facts), key))
        conn.execute("DROP TABLE contour_cursors")
        conn.execute("DROP TABLE analysis_cursors")
        conn.execute("""CREATE TABLE analysis_cursors (source TEXT NOT NULL, instrument_uid TEXT NOT NULL,
            interval TEXT NOT NULL, assignment_id TEXT NOT NULL, open_time_utc TEXT NOT NULL,
            PRIMARY KEY(source,instrument_uid,interval,assignment_id))""")
        conn.execute("INSERT INTO analysis_cursors VALUES('tbank_exchange','u','1h','ng-1h-abcd','2026-10-12T11:00:00Z')")
        conn.execute("UPDATE market_cursors SET processed_through='2026-10-12T11:59:00Z'")
        conn.execute("DELETE FROM metadata WHERE key='contour_marks'")


def test_migration_moves_existing_marks_to_emulation(tmp_path):
    path = tmp_path / "market.sqlite3"
    _legacy(path)
    store = MarketDataStore(path)
    assert store.cursors(**IDENTITY, contour="emulation")["processed_through"] == "2026-10-12T11:59:00Z"
    assert store.analysis_cursor(source="tbank_exchange", instrument_uid="u", interval="1h",
                                 assignment_id="ng-1h-abcd", contour="emulation") == pd.Timestamp("2026-10-12 11:00")
    facts = store.live_state(kind="candle", open_time=OPEN, **IDENTITY)
    assert "input_reference" not in facts and facts["input_references"]["emulation"]["state"] == "started"
    # Начатый пакет продолжается той же ревизией, без повторного исполнения.
    assert store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")["revision"] == 1
    assert list(tmp_path.glob("market.sqlite3.pre-contour-feed-*.bak"))
    # Повторное открытие ничего не переносит заново.
    MarketDataStore(path)
    assert store.cursors(**IDENTITY, contour="emulation")["processed_through"] == "2026-10-12T11:59:00Z"


@pytest.mark.parametrize("call", [
    lambda s: s.mark_processed(open_time=OPEN, **IDENTITY),
    lambda s: s.pin_input(open_time=OPEN, **IDENTITY),
    lambda s: s.mark_analyzed(assignment_id="a", open_time=OPEN, **IDENTITY),
    lambda s: s.analysis_cursor(assignment_id="a", **IDENTITY),
])
def test_mark_without_contour_is_rejected(tmp_path, call):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    with pytest.raises(ValueError, match="контура"):
        call(store)
    assert store.cursors(**IDENTITY)["processed_through"] is None


def test_contour_marks_are_independent(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")
    store.mark_analyzed(assignment_id="a", open_time=OPEN, **IDENTITY, contour="emulation")
    assert store.cursors(**IDENTITY, contour="account")["processed_through"] is None
    assert store.analysis_cursor(assignment_id="a", **IDENTITY, contour="account") is None
    store.mark_processed(open_time=OPEN - pd.Timedelta(minutes=5), **IDENTITY, contour="account")
    assert store.cursors(**IDENTITY, contour="emulation")["processed_through"] == "2026-10-12T12:00:00Z"
    # Диагностика без контура показывает наибольшую границу.
    assert store.cursors(**IDENTITY)["processed_through"] == "2026-10-12T12:00:00Z"


def test_correction_of_minute_passed_by_contour_is_incident_with_contour_id(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.mark_processed(open_time=OPEN + pd.Timedelta(minutes=3), **IDENTITY, contour="emulation")
    store.accept_observation(observation(101, transport="stream"))
    assert not held(store).get("integrity_hold")
    assert held(store)["incident"]["contour_id"] == "emulation"
    assert held(store)["incident"]["reason"] == "legacy_input_unconfirmed"


def test_correction_of_unconsumed_minute_is_published(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.mark_processed(open_time=OPEN - pd.Timedelta(minutes=1), **IDENTITY, contour="emulation")
    store.accept_observation(observation(101, transport="stream"))
    store.accept_observation(observation(101), reconcile_sequence=store.live_state(
        kind="candle", open_time=OPEN, **IDENTITY)["observed_sequence"])
    assert not held(store).get("integrity_hold")
    assert store.current_frame(**IDENTITY)["close"].iloc[-1] == 101


def test_started_pin_of_one_contour_is_consumed_only_by_that_contour(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    store.accept_observation(observation(101, transport="stream"))
    assert held(store)["incident"]["contour_id"] == "emulation" and not held(store).get("integrity_hold")

    def pin_state():
        facts = store.live_state(kind="candle", open_time=OPEN, **IDENTITY)
        return facts["input_references"]["emulation"]["state"]

    store.mark_processed(open_time=OPEN, **IDENTITY, contour="account")
    assert pin_state() == "started"
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")
    assert pin_state() == "consumed"


def test_pair_readiness_is_current_state_not_a_change(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    before = store.source_info().max_change_id
    store.publish_pair_readiness(**IDENTITY, status={"state": "BOOTSTRAPPING"})
    store.publish_pair_readiness(**IDENTITY, status={"state": "READY", "transport": "stream"})
    readiness = store.pair_readiness(**IDENTITY)
    assert readiness["state"] == "READY" and readiness["transport"] == "stream" and readiness["updated_at"]
    assert store.source_info().max_change_id == before
    assert store.pair_readiness(source="tbank_exchange", instrument_uid="other", interval="1m") is None


def test_contour_cursor_holds_retention(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    for minute in range(3):
        store.accept_observation(observation(100, minute=minute))
    producer = store.producer_id
    store.acknowledge("historical-emulator", producer, 3)
    store.acknowledge("contour-emulation", producer, 1)
    result = store.prune_changes(pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1))
    assert result.deleted == 1 and result.holder == "contour-emulation"


def test_http_rejects_reserved_contour_consumer(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    server = MarketDataExportServer(store, "test-key", port=0)
    try:
        server.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        host, port = server.address
        body = json.dumps({"producer_id": store.producer_id, "change_id": 1}).encode()
        req = request.Request(f"http://{host}:{port}/api/v1/market-data/consumers/contour-emulation/checkpoint",
                              data=body, headers={"Authorization": "Bearer test-key"}, method="PUT")
        with pytest.raises(error.HTTPError) as exc:
            request.urlopen(req, timeout=2)
        assert exc.value.code == 400
    finally:
        server.close()
    assert store.source_info().max_change_id == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumer_checkpoints").fetchone()[0] == 0
