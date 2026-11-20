"""Открытие актуальной базы свечей не ждёт пишущий процесс (fix-market-store-startup-lock).

18.11–19.11.2026: ядро и контуры стартуют одновременно; «Счёт» падал с
``database is locked``, потому что открытие базы каждый раз брало блокировку
записи (снятие удержаний, версия схемы, значения по умолчанию).
"""

import json
import sqlite3
import time
from contextlib import closing

from src.data.market_store import SCHEMA_VERSION, MarketDataStore


def metadata(path, key):
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def test_open_of_current_database_does_not_wait_for_writer(tmp_path):
    path = tmp_path / "market-data.sqlite3"
    MarketDataStore(path)
    producer = metadata(path, "producer_id")
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")              # ядро держит блокировку записи
        started = time.monotonic()
        MarketDataStore(path)
        elapsed = time.monotonic() - started
        writer.execute("ROLLBACK")
    assert elapsed < 2
    assert metadata(path, "producer_id") == producer   # значения по умолчанию не перезаписаны


def test_missing_metadata_is_still_written_on_open(tmp_path):
    path = tmp_path / "market-data.sqlite3"
    MarketDataStore(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
        conn.execute("DELETE FROM metadata WHERE key IN ('producer_id','min_after_id')")
    MarketDataStore(path)
    assert metadata(path, "schema_version") == str(SCHEMA_VERSION)
    assert metadata(path, "producer_id")
    assert metadata(path, "min_after_id") == "0"


def test_legacy_hold_is_still_released_on_open(tmp_path):
    path = tmp_path / "market-data.sqlite3"
    MarketDataStore(path)
    key = MarketDataStore._live_key("uid", "tbank_exchange", "u")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (key, json.dumps({"integrity_hold": True})))
    MarketDataStore(path)
    assert "integrity_hold" not in json.loads(metadata(path, key))
