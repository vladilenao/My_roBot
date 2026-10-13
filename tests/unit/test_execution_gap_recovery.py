"""Recovery priority for a live position whose execution cursor is behind."""

from datetime import datetime, timedelta

import pandas as pd

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


def _candles(*times):
    return pd.DataFrame({
        "datetime": pd.to_datetime(times), "open": [100] * len(times),
        "high": [101] * len(times), "low": [99] * len(times),
        "close": [100] * len(times), "volume": [1] * len(times),
        "is_complete": [True] * len(times),
    })


def _worker(tmp_path, clock, *, recover):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    uid = "uid-ed"
    source = "tbank_exchange"
    first = "2026-10-08 11:19"
    missing = "2026-10-08 11:20"
    later = "2026-10-08 11:21"
    store.record_candles(source=source, instrument_uid=uid, interval="1m",
                         candles=_candles(first, later))
    store.set_interval_state(source=source, instrument_uid=uid, interval="1m",
                             open_time=missing, state="unresolved", evidence="schedule_unavailable")
    # Earlier archival gaps are unrelated to this position's financial cursor.
    for minute in range(100):
        store.set_interval_state(source=source, instrument_uid=uid, interval="1m",
            open_time=pd.Timestamp("2026-09-14 04:00") + pd.Timedelta(minutes=minute),
            state="unresolved", evidence="schedule_unavailable")
    calls = []

    def loader(**kwargs):
        calls.append(pd.Timestamp(kwargs["start_date"]))
        return (_candles(missing) if recover else _candles()), uid

    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0])
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0),
                            market_store=store)
    item = Instrument("ED", "EDZ6", "future", "ED-12.26", uid)
    cache.note_execution_lower(item, pd.Timestamp(first))
    cache.register_live_pair(item, uid, "1m")
    cache.mark_live_history(uid, "1m", done=True)
    worker = IngestionWorker(cache)
    worker.set_execution_obligations({uid: (datetime(2026, 10, 8, 11, 19), False)})
    return worker, store, calls, missing


def test_open_position_gap_is_recovered_before_old_archive(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, store, calls, missing = _worker(tmp_path, clock, recover=True)

    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == [pd.Timestamp(missing)]
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-ed",
                                interval="1m", open_time=missing) == "received"
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-ed",
                                interval="1m", open_time="2026-09-14 04:00") == "unresolved"


def test_empty_old_minute_stays_unresolved_and_obeys_cooldown(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, store, calls, missing = _worker(tmp_path, clock, recover=False)

    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-ed",
                                interval="1m", open_time=missing) == "unresolved"
    assert calls == [pd.Timestamp(missing)]
    assert not worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == [pd.Timestamp(missing)]
    clock[0] += timedelta(seconds=worker.settings.recovery_recheck_interval_secs)
    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == [pd.Timestamp(missing), pd.Timestamp(missing)]


def test_no_coverage_row_is_still_an_execution_gap(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, store, calls, missing = _worker(tmp_path, clock, recover=True)
    with store._connect() as conn:
        conn.execute("DELETE FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                     ("tbank_exchange", "uid-ed", "1m", "2026-10-08T11:20:00Z"))

    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == [pd.Timestamp(missing)]
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-ed",
                                interval="1m", open_time=missing) == "received"


def test_request_failure_on_missing_coverage_row_gets_durable_cooldown(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, store, _, missing = _worker(tmp_path, clock, recover=True)
    with store._connect() as conn:
        conn.execute("DELETE FROM interval_coverage WHERE source=? AND instrument_uid=? AND interval=? AND open_time_utc=?",
                     ("tbank_exchange", "uid-ed", "1m", "2026-10-08T11:20:00Z"))
    calls = []

    def failing_loader(**kwargs):
        calls.append(kwargs["start_date"])
        raise RuntimeError("temporary source failure")

    worker._cache._loader = failing_loader

    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid-ed",
                                interval="1m", open_time=missing) == "unresolved"
    assert not worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert len(calls) == 1


def test_closed_position_removes_urgent_recovery_without_financial_db_read(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, _, calls, _ = _worker(tmp_path, clock, recover=True)

    worker.set_execution_obligations({})

    assert not worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == []


def test_urgent_recovery_rotates_between_two_active_instruments(tmp_path):
    clock = [datetime(2026, 10, 13, 12, 3)]
    worker, store, _, missing = _worker(tmp_path, clock, recover=True)
    second = Instrument("Si", "SIZ6", "future", "Si-12.26", "uid-si")
    store.record_candles(source="tbank_exchange", instrument_uid="uid-si", interval="1m",
                         candles=_candles("2026-10-08 11:19", "2026-10-08 11:21"))
    store.set_interval_state(source="tbank_exchange", instrument_uid="uid-si", interval="1m",
                             open_time=missing, state="unresolved", evidence="schedule_unavailable")
    worker._cache.note_execution_lower(second, pd.Timestamp("2026-10-08 11:19"))
    worker._cache.register_live_pair(second, "uid-si", "1m")
    worker._cache.mark_live_history("uid-si", "1m", done=True)
    worker.set_execution_obligations({uid: (datetime(2026, 10, 8, 11, 19), False)
                                      for uid in ("uid-ed", "uid-si")})
    calls = []

    def loader(**kwargs):
        calls.append(kwargs["instrument_id"])
        return _candles(missing), kwargs["instrument_id"]

    worker._cache._loader = loader

    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert worker._service_execution_gap(pd.Timestamp(clock[0]))
    assert calls == ["uid-ed", "uid-si"]
