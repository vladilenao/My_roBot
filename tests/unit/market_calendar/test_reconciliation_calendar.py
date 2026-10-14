from datetime import date, datetime

import pandas as pd

from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar, expect


def _frame(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps)})


class _Calendar:
    def __init__(self, calendar):
        self._calendar = calendar

    def calendar_for(self, uid, now):
        return self._calendar

    def expectation(self, uid, rule, now, timeout):
        return expect(self._calendar, rule, now, timeout, instrument_uid=uid)

    def service(self, now):
        return False


def _calendar(*windows, trading=True):
    return TradingCalendar(
        "tbank", "MOEX",
        (CalendarDay(date(2026, 10, 13), trading, tuple(windows), "version-1", datetime(2026, 10, 12)),),
    )


def test_calendar_expected_sequence_marks_only_trading_gap_pending(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    calendar = _calendar(SessionWindow(datetime(2026, 10, 13, 10), datetime(2026, 10, 13, 11), "continuous", "main"))
    reconciler = MarketDataReconciler(store, source="tbank_exchange", market_calendar=_Calendar(calendar))

    reconciler.reconcile(instrument_uid="uid", interval="1m", candles=_frame("2026-10-13 10:00", "2026-10-13 10:02"), now=datetime(2026, 10, 13, 10, 3))

    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time="2026-10-13 10:01") == "pending"


def test_calendar_expected_sequence_includes_trailing_gap_without_next_candle(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    calendar = _calendar(SessionWindow(datetime(2026, 10, 13, 10), datetime(2026, 10, 13, 11), "continuous", "main"))
    reconciler = MarketDataReconciler(store, source="tbank_exchange", market_calendar=_Calendar(calendar))

    reconciler.reconcile(instrument_uid="uid", interval="1m", candles=_frame("2026-10-13 10:00"), now=datetime(2026, 10, 13, 10, 3))

    assert [store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time=f"2026-10-13 10:0{minute}") for minute in (1, 2)] == ["pending", "pending"]


def test_calendar_evidence_controls_scheduled_closed(tmp_path):
    closed = _calendar(trading=False)
    for enabled, expected in ((True, "scheduled_closed"), (False, "unresolved")):
        store = MarketDataStore(tmp_path / f"{enabled}.sqlite3")
        reconciler = MarketDataReconciler(
            store, source="tbank_exchange", market_calendar=_Calendar(closed), calendar_evidence=enabled,
        )

        reconciler.reconcile(instrument_uid="uid", interval="1m", candles=_frame("2026-10-13 10:00", "2026-10-13 10:02"), now=datetime(2026, 10, 13, 10, 3))

        assert store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time="2026-10-13 10:01") == expected


def test_received_candle_overrides_scheduled_closed(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.set_interval_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time="2026-10-13 10:01", state="scheduled_closed", evidence="calendar")

    store.record_candles(
        source="tbank_exchange", instrument_uid="uid", interval="1m",
        candles=pd.DataFrame({"datetime": [pd.Timestamp("2026-10-13 10:01")], "open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "is_complete": [True]}),
    )

    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time="2026-10-13 10:01") == "received"
