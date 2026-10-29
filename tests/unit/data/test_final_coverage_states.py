"""Итог по данным источника не откатывается в ожидание (fix-pending-overwrites-resolved).

19.10.2026: минуты ночной дилерской сессии PHOR 17.10 после 00:02 получали
«нет сделок» (GetLastTrades), а затем «у источника нет», но каждая новая
сверка с расписанием снова писала поверх ``pending``. Сопровождение открытой
сделки замирало на 00:03, риск-учёт отказывал во всех новых входах.
"""

import json
from datetime import date, datetime

import pandas as pd

from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar, expect

SOURCE = "tbank_exchange"
UID = "uid-phor"
NIGHT = SessionWindow(datetime(2026, 10, 17, 0, 0), datetime(2026, 10, 17, 3, 0), "continuous", "dealer")
ABSENT = "source_absent:checks=3:first=2026-10-17T01:03:00Z:last=2026-10-18T17:25:00Z"


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
        CalendarDay(date(2026, 10, 17), True, (NIGHT,), "version-1", datetime(2026, 10, 16)),))
    return MarketDataReconciler(store, source=SOURCE, market_calendar=_Calendar(calendar))


def _frame(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps)})


def _candles(*stamps):
    return pd.DataFrame({
        "datetime": pd.to_datetime(stamps), "open": [1] * len(stamps), "high": [1] * len(stamps),
        "low": [1] * len(stamps), "close": [1] * len(stamps), "volume": [1] * len(stamps),
        "is_complete": [True] * len(stamps),
    })


def _state(store, stamp):
    return store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time=stamp)


def _states(store, *stamps):
    return [_state(store, stamp) for stamp in stamps]


def test_tail_rescan_keeps_confirmed_no_trade(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    reconciler = _reconciler(store)
    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=_frame("2026-10-17 00:02"),
                         now=datetime(2026, 10, 17, 0, 5, 5))
    assert _states(store, "2026-10-17 00:03", "2026-10-17 00:04") == ["pending", "pending"]
    store.set_interval_states(source=SOURCE, instrument_uid=UID, interval="1m", observed_at="2026-10-17 00:05:06",
                              rows=[("2026-10-17 00:03", "confirmed_no_trade", "last_trades_empty"),
                                    ("2026-10-17 00:04", "confirmed_no_trade", "last_trades_empty")])

    # Новый хвост «последняя свеча → последний срочный бар» снова покрывает 00:03.
    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=_frame("2026-10-17 00:02"),
                         now=datetime(2026, 10, 17, 0, 7, 50))

    assert _states(store, "2026-10-17 00:03", "2026-10-17 00:04", "2026-10-17 00:05") == [
        "confirmed_no_trade", "confirmed_no_trade", "pending"]


def test_restart_rescan_keeps_source_absent_and_feed_has_no_downgrade(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m",
                         candles=_candles("2026-10-17 00:02", "2026-10-17 01:26"))
    store.set_interval_states(source=SOURCE, instrument_uid=UID, interval="1m", observed_at="2026-10-18 17:25",
                              rows=[("2026-10-17 00:03", "source_absent", ABSENT)])
    with store._connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM changes WHERE kind='interval_status'").fetchone()[0]

    # Новый процесс: маркеры просмотренных диапазонов в памяти пусты.
    _reconciler(store).reconcile(instrument_uid=UID, interval="1m",
                                 candles=_frame("2026-10-17 00:02", "2026-10-17 01:26"),
                                 now=datetime(2026, 10, 19, 4, 25, 3))

    assert _state(store, "2026-10-17 00:03") == "source_absent"
    assert _state(store, "2026-10-17 00:04") == "pending"
    with store._connect() as conn:
        rows = conn.execute("SELECT payload_json FROM changes WHERE kind='interval_status' LIMIT -1 OFFSET ?",
                            (before,)).fetchall()
    downgraded = [json.loads(row[0])["interval_status"] for row in rows]
    assert not [item for item in downgraded if item["open_time_utc"] == "2026-10-17T00:03:00Z"]


def test_single_write_does_not_downgrade_final_state(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:03",
                             state="confirmed_no_trade", evidence="last_trades_empty")
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:04",
                             state="source_absent", evidence=ABSENT)

    assert store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:03",
                                    state="pending", evidence="open_session_missing_candle") is None
    assert store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:04",
                                    state="unresolved", evidence="empty_recovery_response") is None

    assert _states(store, "2026-10-17 00:03", "2026-10-17 00:04") == ["confirmed_no_trade", "source_absent"]


def test_candle_still_overrides_final_state(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:03",
                             state="source_absent", evidence=ABSENT)

    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=_candles("2026-10-17 00:03"))

    assert _state(store, "2026-10-17 00:03") == "received"


def test_calendar_can_still_reopen_scheduled_closed(tmp_path):
    """Плановый перерыв — вывод из расписания: новая версия расписания его пересматривает."""
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-17 00:03",
                             state="scheduled_closed", evidence="applicable_trading_schedule:old")

    _reconciler(store).reconcile(instrument_uid=UID, interval="1m", candles=_frame("2026-10-17 00:02"),
                                 now=datetime(2026, 10, 17, 0, 5, 5))

    assert _state(store, "2026-10-17 00:03") == "pending"


def test_prefix_passes_proven_minutes_after_rescan(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m",
                         candles=_candles("2026-10-17 00:02", "2026-10-17 00:06"))
    store.set_interval_states(source=SOURCE, instrument_uid=UID, interval="1m", observed_at="2026-10-17 00:05",
                              rows=[("2026-10-17 00:03", "confirmed_no_trade", "last_trades_empty"),
                                    ("2026-10-17 00:04", "confirmed_no_trade", "last_trades_empty"),
                                    ("2026-10-17 00:05", "source_absent", ABSENT)])

    _reconciler(store).reconcile(instrument_uid=UID, interval="1m",
                                 candles=_frame("2026-10-17 00:02", "2026-10-17 00:06"),
                                 now=datetime(2026, 10, 19, 4, 25, 3))

    assert store.resolve_contiguous_prefix(source=SOURCE, instrument_uid=UID, interval="1m",
                                           meaningful_lower="2026-10-17 00:02",
                                           horizon="2026-10-17 00:06") == pd.Timestamp("2026-10-17 00:06")
