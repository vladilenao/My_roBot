"""Правка исполненной минуты — инцидент без удержания UID (fix-executed-minute-revision)."""

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from src.data.live_state import normalize_rpc_candle
from src.data.market_store import MarketDataStore

IDENTITY = dict(source="tbank_exchange", instrument_uid="u", interval="1m")
OPEN = datetime(2026, 10, 29, 15, 59)


def observation(price=100, *, transport="rpc", minute=0, volume=1):
    stamp = OPEN + pd.Timedelta(minutes=minute)
    row = dict(datetime=stamp, open=price, high=price + 1, low=price - 1, close=price, volume=volume, is_complete=True)
    received = (stamp + pd.Timedelta(minutes=1, seconds=2)).isoformat() + "Z"
    result = normalize_rpc_candle(row, uid="u", timeframe="1m", received_at=received, actual_source=1)
    return replace(result, transport=transport)


def held(store):
    return store.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u")


def executed_minute(store):
    store.accept_observation(observation(100))
    store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")


def write_uid_state(path, value):
    with sqlite3.connect(path) as conn:
        key = MarketDataStore._live_key("uid", IDENTITY["source"], "u")
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, json.dumps(value)))


def test_volume_correction_of_executed_minute_does_not_stop_next_minute(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    executed_minute(store)
    # 29.10: поток прислал ту же минуту позже с бóльшим объёмом.
    result = store.accept_observation(observation(100, transport="stream", volume=7))
    assert result["state"] == "conflict" and result["incident"]["contour_id"] == "emulation"
    assert not held(store).get("integrity_hold")
    store.accept_observation(observation(101, minute=1))
    reference = store.pin_input(open_time=OPEN + pd.Timedelta(minutes=1), **IDENTITY, contour="emulation")
    assert reference["state"] == "started"


def test_repeated_correction_is_one_incident(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    executed_minute(store)
    assert store.accept_observation(observation(99, transport="stream"))["incident"]
    assert store.accept_observation(observation(99, transport="stream"))["incident"] is None


def test_unpinned_minute_in_conflict_still_waits_for_reconciliation(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.accept_observation(observation(101, transport="stream"))
    with pytest.raises(ValueError, match="неподтверждённый"):
        store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    assert held(store) == {}


@pytest.mark.parametrize("legacy", [
    {"integrity_hold": True, "incident": {"open_time": "2026-10-29T15:59:00Z", "candidate_revision": 2,
                                          "consumed_revision": 1, "reason": "consumed_input_changed",
                                          "contour_id": "emulation", "received_at": "2026-10-29T16:01:25Z"}},
    {"pending_input_conflicts": {"2026-10-29T15:59:00Z": {"revision": 1, "candidate_revision": 2,
                                                          "contour_id": "emulation"}}},
], ids=["integrity_hold", "pending_input_conflicts"])
def test_hold_of_previous_version_is_released_on_open(tmp_path, legacy):
    path = tmp_path / "market.sqlite3"
    executed_minute(MarketDataStore(path))
    write_uid_state(path, legacy)
    store = MarketDataStore(path)
    state = held(store)
    assert "integrity_hold" not in state and "pending_input_conflicts" not in state
    incident = state["incident"]
    assert incident["open_time"] == "2026-10-29T15:59:00Z" and incident["contour_id"] == "emulation"
    assert incident["released_at"]
    store.accept_observation(observation(101, minute=1))
    store.pin_input(open_time=OPEN + pd.Timedelta(minutes=1), **IDENTITY, contour="emulation")
    # Повторное открытие ничего не переписывает.
    assert held(MarketDataStore(path)) == state


def test_core_warns_once_about_incident(tmp_path, caplog):
    from src.data.cache import MarketDataCache
    from src.data.request_budget import RequestBudget
    from src.instruments import Instrument
    from src.scheduler.timing import MultiTimeframeScheduler

    now = datetime(2026, 10, 29, 16, 2)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), "u"), MultiTimeframeScheduler(["1m"], clock=lambda: now),
                            request_budget=RequestBudget(lambda: now, 0), market_store=store)
    cache.register_live_pair(Instrument("BRX6", "BRX6", "future", "BRX6", "u"), "u", "1m")
    executed_minute(store)
    frame = pd.DataFrame([dict(datetime=pd.Timestamp(OPEN), open=99, high=100, low=98, close=99, volume=1,
                               is_complete=True)])
    with caplog.at_level(logging.WARNING, logger="src.data.cache"):
        cache.accept_rpc_frame("u", "1m", frame, received_at=now)
        cache.accept_rpc_frame("u", "1m", frame, received_at=now)
    warnings = [record for record in caplog.records if "исправил исполненную минуту" in record.getMessage()]
    assert len(warnings) == 1
