"""Пакет состояний покрытия без изменений не пишет в базу (fix-coverage-reconcile-restart).

19.11.2026: после перезапуска ядра сверка заново проходила все ~3 тыс.
промежутков ряда 1m пары, на каждый брала блокировку записи и читала минуты по
одной. Состояния уже были записаны, а эмуляция ждала прогрева 5–7 минут.
"""

import sqlite3
import time
from datetime import date, datetime

import pandas as pd
import pytest

from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar, expect

SOURCE = "tbank_exchange"
UID = "uid-test"
WINDOW = SessionWindow(datetime(2026, 10, 17, 0, 0), datetime(2026, 10, 17, 3, 0), "continuous", "dealer")


class _Calendar:
    def __init__(self, calendar):
        self._calendar = calendar

    def calendar_for(self, uid, now):
        return self._calendar

    def expectation(self, uid, rule, now, timeout):
        return expect(self._calendar, rule, now, timeout, instrument_uid=uid)

    def service(self, now):
        return False


def _reconciler(store):
    calendar = TradingCalendar("tbank", "MOEX", (
        CalendarDay(date(2026, 10, 17), True, (WINDOW,), "version-1", datetime(2026, 10, 16)),))
    return MarketDataReconciler(store, source=SOURCE, market_calendar=_Calendar(calendar))


def _candles(*stamps):
    return pd.DataFrame({
        "datetime": pd.to_datetime(stamps), "open": [1] * len(stamps), "high": [1] * len(stamps),
        "low": [1] * len(stamps), "close": [1] * len(stamps), "volume": [1] * len(stamps),
        "is_complete": [True] * len(stamps),
    })


def _changes(store):
    with store._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]


def _state(store, stamp):
    return store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time=stamp)


def _batch(store, rows):
    return store.set_interval_states(source=SOURCE, instrument_uid=UID, interval="1m", rows=rows,
                                     observed_at="2026-10-17 01:00")


@pytest.fixture
def write_lock(tmp_path):
    """Другой процесс держит блокировку записи рыночной базы."""
    holders = []

    def hold(store):
        conn = sqlite3.connect(store.path, timeout=1, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        holders.append(conn)

    yield hold
    for conn in holders:
        conn.rollback()
        conn.close()


PENDING = [("2026-10-17 00:03", "pending", "open_session_missing_candle"),
           ("2026-10-17 00:04", "pending", "open_session_missing_candle")]


def test_repeat_batch_does_not_wait_for_foreign_write_lock(tmp_path, write_lock):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    assert _batch(store, PENDING) == 2
    before = _changes(store)
    write_lock(store)

    started = time.monotonic()
    assert _batch(store, PENDING) == 0
    assert time.monotonic() - started < 2
    assert _changes(store) == before


def test_batch_writes_only_changed_rows(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    _batch(store, PENDING[:1])
    before = _changes(store)

    assert _batch(store, PENDING) == 1

    assert _changes(store) == before + 1
    assert _state(store, "2026-10-17 00:04") == "pending"


def test_batch_keeps_received_and_final_states(tmp_path, write_lock):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=_candles("2026-10-17 00:03"))
    _batch(store, [("2026-10-17 00:04", "confirmed_no_trade", "last_trades_empty")])
    before = _changes(store)
    write_lock(store)

    assert _batch(store, PENDING) == 0

    assert [_state(store, "2026-10-17 00:03"), _state(store, "2026-10-17 00:04")] == ["received", "confirmed_no_trade"]
    assert _changes(store) == before


def test_prefix_advances_without_changed_rows(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.save_live_state({"coverage_lower": "2026-10-17T00:02:00"}, kind="pair", source=SOURCE,
                          instrument_uid=UID, interval="1m")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m",
                         candles=_candles("2026-10-17 00:02", "2026-10-17 00:04"))
    # Состояние записано путём без пересчёта префикса.
    with store._connect() as conn:
        conn.execute("""INSERT INTO interval_coverage(source,instrument_uid,interval,open_time_utc,state,evidence,observed_at)
            VALUES (?,?,?,?,?,?,?)""", (SOURCE, UID, "1m", "2026-10-17T00:03:00Z", "scheduled_closed", "clearing",
                                        "2026-10-17T01:00:00Z"))

    assert _batch(store, [("2026-10-17 00:03", "scheduled_closed", "clearing")]) == 0

    with store._connect() as conn:
        resolved = conn.execute("SELECT resolved_through FROM market_cursors WHERE instrument_uid=?", (UID,)).fetchone()[0]
    assert resolved == "2026-10-17T00:04:00Z"


def _gappy_frame():
    stamps = [f"2026-10-17 00:{minute:02d}" for minute in range(0, 60, 3)]
    return _candles(*stamps)


def test_reconcile_writes_series_in_one_batch_and_restart_rescan_skips_lock(tmp_path, write_lock, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    frame = _gappy_frame()
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=frame)
    calls = []
    original = store.set_interval_states
    monkeypatch.setattr(store, "set_interval_states", lambda **kwargs: calls.append(1) or original(**kwargs))
    now = datetime(2026, 10, 17, 1, 0, 5)

    _reconciler(store).reconcile(instrument_uid=UID, interval="1m", candles=frame, now=now)

    assert len(calls) == 1
    assert _state(store, "2026-10-17 00:01") == "pending"
    before = _changes(store)
    write_lock(store)

    # Новый процесс: маркеры просмотренных промежутков в памяти пусты.
    started = time.monotonic()
    _reconciler(store).reconcile(instrument_uid=UID, interval="1m", candles=frame, now=now)

    assert time.monotonic() - started < 2
    assert len(calls) == 2
    assert _changes(store) == before


def test_failed_batch_leaves_ranges_for_retry(tmp_path, monkeypatch):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    frame = _gappy_frame()
    reconciler = _reconciler(store)
    original = store.set_interval_states

    def fail(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "set_interval_states", fail)
    with pytest.raises(sqlite3.OperationalError):
        reconciler.reconcile(instrument_uid=UID, interval="1m", candles=frame, now=datetime(2026, 10, 17, 1, 0, 5))
    monkeypatch.setattr(store, "set_interval_states", original)

    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=frame, now=datetime(2026, 10, 17, 1, 0, 5))

    assert _state(store, "2026-10-17 00:01") == "pending"
