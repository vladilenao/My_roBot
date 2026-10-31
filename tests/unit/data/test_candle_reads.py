"""Чтение рядов и окна свечей рыночной базы для API графиков (add-candles-api)."""

import sqlite3

import pandas as pd
import pytest

from src.data.market_store import SOURCE_TBANK, MarketDataStore

START = pd.Timestamp("2026-10-30 06:00")


def _candle(minutes: int, close: float, complete: bool = True) -> dict:
    return {"datetime": START + pd.Timedelta(minutes=minutes), "open": close - 0.01, "high": close + 0.02,
            "low": close - 0.02, "close": close, "volume": 100 + minutes, "is_complete": complete}


@pytest.fixture
def store(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.save_roster(source=SOURCE_TBANK, entries=[
        {"instrument_uid": "uid-ng12", "interval": "1m", "ticker": "NGZ6", "instrument_type": "future",
         "short_name": "NG-12.26", "origin": "selected"},
        {"instrument_uid": "uid-ng12", "interval": "15m", "ticker": "NGZ6", "instrument_type": "future",
         "short_name": "NG-12.26", "origin": "selected"},
        {"instrument_uid": "uid-ng11", "interval": "15m", "ticker": "NGX6", "instrument_type": "future",
         "short_name": "NG-11.26", "origin": "obligation"},
    ])
    store.mark_roster_previous(source=SOURCE_TBANK, pairs=[("uid-ng11", "15m")])
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m",
                         candles=[_candle(15 * index, 3.40 + index / 1000) for index in range(10)])
    return store


def _window(store, lower=None, upper=START + pd.Timedelta(days=1), limit=500, interval="15m"):
    return store.candle_window(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval=interval,
                               lower=lower, upper=upper, limit=limit)


class TestWindow:
    def test_last_candles_ascending_with_earlier(self, store):
        candles, earlier = _window(store, limit=3)
        assert [item["open_time"] for item in candles] == ["2026-10-30T07:45:00Z", "2026-10-30T08:00:00Z",
                                                           "2026-10-30T08:15:00Z"]
        assert earlier is True
        assert candles[-1] == {"open_time": "2026-10-30T08:15:00Z", "open": "3.399", "high": "3.429",
                               "low": "3.389", "close": "3.409", "volume": 235, "complete": True}

    def test_whole_series_has_no_earlier(self, store):
        candles, earlier = _window(store, limit=50)
        assert len(candles) == 10 and earlier is False

    def test_bounds_are_half_open(self, store):
        candles, earlier = _window(store, lower=START + pd.Timedelta(minutes=30), upper=START + pd.Timedelta(minutes=75))
        assert [item["open_time"][11:16] for item in candles] == ["06:30", "06:45", "07:00"]
        assert earlier is True

    def test_empty_window_points_back_when_series_is_older(self, store):
        later = START + pd.Timedelta(days=2)
        assert _window(store, lower=later, upper=later + pd.Timedelta(hours=1)) == ([], True)
        assert _window(store, upper=START) == ([], False)

    def test_only_trailing_incomplete_candle(self, store):
        store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="1m",
                             candles=[_candle(0, 3.40), _candle(1, 3.41, complete=False), _candle(2, 3.42),
                                      _candle(3, 3.43, complete=False)])
        candles, _ = _window(store, interval="1m")
        assert [(item["open_time"][11:16], item["complete"]) for item in candles] == [
            ("06:00", True), ("06:02", True), ("06:03", False)]

    def test_no_incomplete_after_completion(self, store):
        store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="1m",
                             candles=[_candle(0, 3.40), _candle(1, 3.41, complete=False)])
        store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="1m", candles=[_candle(1, 3.415)])
        candles, _ = _window(store, interval="1m")
        assert [item["complete"] for item in candles] == [True, True]


class TestSeries:
    def test_series_with_edges_and_readiness(self, store):
        store.publish_pair_readiness(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m",
                                     status={"state": "READY", "ticker": "NGZ6"})
        rows = {(row["short_name"], row["interval"]): row for row in store.candle_series(source=SOURCE_TBANK)}
        assert set(rows) == {("NG-12.26", "1m"), ("NG-12.26", "15m"), ("NG-11.26", "15m")}
        ng = rows[("NG-12.26", "15m")]
        assert (ng["first_open"], ng["last_open"], ng["last_close"], ng["readiness"], ng["origin"]) == (
            "2026-10-30T06:00:00Z", "2026-10-30T08:15:00Z", "3.409", "READY", "selected")
        empty = rows[("NG-12.26", "1m")]
        assert (empty["first_open"], empty["last_close"], empty["readiness"]) == (None, None, None)
        assert rows[("NG-11.26", "15m")]["origin"] == "previous"


def test_reads_do_not_touch_the_store(store):
    def snapshot():
        with sqlite3.connect(store.path) as conn:
            return [conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
                    for table in ("market_cursors", "interval_coverage")] + \
                   [conn.execute("SELECT MAX(change_id) FROM changes").fetchone()]

    before = snapshot()
    for _ in range(20):
        store.candle_series(source=SOURCE_TBANK)
        _window(store, limit=2)
    assert snapshot() == before
