"""Хранилище полос: схема 4, «у источника нет», состав пар, отметки проверок."""

import sqlite3
from datetime import datetime

import pandas as pd

from src.data.market_store import MarketDataStore

SOURCE = "tbank_exchange"


def candles(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(list(stamps)), "open": [1.0] * len(stamps), "high": [1.0] * len(stamps),
                         "low": [1.0] * len(stamps), "close": [1.0] * len(stamps), "volume": [1] * len(stamps),
                         "is_complete": [True] * len(stamps)})


def test_schema3_database_is_migrated_with_rows_kept_and_backup(tmp_path):
    path = tmp_path / "market.sqlite3"
    store = MarketDataStore(path)
    store.set_interval_state(source=SOURCE, instrument_uid="u", interval="1m", open_time="2026-10-12 12:01",
                             state="unresolved", evidence="schedule_unavailable")
    with sqlite3.connect(path) as conn:  # возвращаем таблицу к CHECK схемы 3
        conn.executescript("""
            ALTER TABLE interval_coverage RENAME TO c_new;
            CREATE TABLE interval_coverage (source TEXT NOT NULL, instrument_uid TEXT NOT NULL, interval TEXT NOT NULL,
                open_time_utc TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('received', 'scheduled_closed', 'confirmed_no_trade', 'pending', 'unresolved')),
                evidence TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY (source, instrument_uid, interval, open_time_utc));
            INSERT INTO interval_coverage SELECT * FROM c_new; DROP TABLE c_new;""")
    migrated = MarketDataStore(path)
    assert migrated.coverage_state(source=SOURCE, instrument_uid="u", interval="1m",
                                   open_time="2026-10-12 12:01") == "unresolved"
    migrated.set_interval_state(source=SOURCE, instrument_uid="u", interval="1m", open_time="2026-10-12 12:01",
                                state="source_absent", evidence="source_absent:checks=3")
    assert migrated.coverage_state(source=SOURCE, instrument_uid="u", interval="1m",
                                   open_time="2026-10-12 12:01") == "source_absent"
    assert list(tmp_path.glob("market.sqlite3.before-schema-4-*.bak"))
    assert migrated.source_info().schema_version == 4


def test_source_absent_is_exported_as_unresolved_and_resolves_prefix(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid="u", interval="1m",
                         candles=candles("2026-10-12 12:00", "2026-10-12 12:02"))
    store.set_interval_state(source=SOURCE, instrument_uid="u", interval="1m", open_time="2026-10-12 12:01",
                             state="source_absent", evidence="source_absent:checks=3:first=x:last=y")
    status = [row["interval_status"] for row in store.changes_after(store.producer_id, 0)["changes"]
              if row["kind"] == "interval_status"][-1]
    assert status["state"] == "unresolved" and status["evidence"].startswith("source_absent:")
    store.resolve_contiguous_prefix(source=SOURCE, instrument_uid="u", interval="1m",
                                    meaningful_lower="2026-10-12 12:00", horizon="2026-10-12 12:02")
    resolved = store.cursors(source=SOURCE, instrument_uid="u", interval="1m")["resolved_through"]
    assert pd.Timestamp(resolved).tz_convert(None) == pd.Timestamp("2026-10-12 12:02")


def test_roster_keeps_obligations_and_retires_unselected_pairs(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    pair = lambda uid, origin: {"instrument_uid": uid, "interval": "1h", "ticker": uid.upper(),
                                "instrument_type": "share", "origin": origin}
    store.save_roster(source=SOURCE, entries=[pair("sber", "selected"), pair("phor", "selected"), pair("ng", "obligation")])
    store.save_roster(source=SOURCE, entries=[pair("sber", "selected")])
    current = {row["instrument_uid"]: row["origin"] for row in store.roster(source=SOURCE)}
    assert current == {"sber": "selected", "ng": "obligation"}   # phor выбыл, но обязательство ng осталось
    previous = store.roster(source=SOURCE, origins=("previous",))
    assert [row["instrument_uid"] for row in previous] == ["phor"]
    store.set_roster_window(source=SOURCE, instrument_uid="sber", interval="1h", window_lower="2026-10-01 10:00")
    assert store.roster(source=SOURCE)[1]["window_lower"] == pd.Timestamp("2026-10-01 10:00")


def test_interval_checks_count_and_window_snapshot(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid="u", interval="1h", candles=candles("2026-10-12 10:00"))
    store.set_interval_state(source=SOURCE, instrument_uid="u", interval="1h", open_time="2026-10-12 11:00",
                             state="unresolved", evidence="empty_recovery_response")
    holes = [pd.Timestamp("2026-10-12 11:00"), pd.Timestamp("2026-10-12 12:00")]
    store.record_interval_checks(source=SOURCE, instrument_uid="u", interval="1h", open_times=holes,
                                 checked_at=datetime(2026, 10, 13, 9), next_check_at=datetime(2026, 10, 13, 10))
    store.record_interval_checks(source=SOURCE, instrument_uid="u", interval="1h", open_times=holes[:1],
                                 checked_at=datetime(2026, 10, 13, 10), next_check_at=None)
    snap = store.window_snapshot(source=SOURCE, instrument_uid="u", interval="1h",
                                 lower="2026-10-12 10:00", upper="2026-10-12 12:00")
    assert snap["received"] == {pd.Timestamp("2026-10-12 10:00")}
    assert snap["states"][pd.Timestamp("2026-10-12 11:00")][0] == "unresolved"
    first = snap["checks"][pd.Timestamp("2026-10-12 11:00")]
    assert first["checks"] == 2 and first["next_check_at"] is None
    assert first["first_checked_at"] == pd.Timestamp("2026-10-13 09:00")
    assert snap["checks"][pd.Timestamp("2026-10-12 12:00")]["checks"] == 1
