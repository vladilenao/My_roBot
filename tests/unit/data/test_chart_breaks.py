"""Классификация разрывов окна графика: покрытие и известный календарь, без сети и записи."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.instruments import Instrument
from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar
from src.scheduler.timing import MultiTimeframeScheduler

SOURCE = "tbank_exchange"
ITEM = Instrument("Лукойл", "LKOH", "share", "LKOH", "uid")


class _Calendar:
    """Сохранённый календарь; запрос загрузки — ошибка теста."""

    def __init__(self, calendar):
        self._calendar = calendar
        self.requests = 0

    def exchange_for(self, uid):
        return "MOEX" if self._calendar else None

    def calendar(self, exchange, now):
        return self._calendar

    def calendar_for(self, uid, now):
        self.requests += 1
        raise AssertionError("классификация графика не должна ставить загрузку расписания")


def _day(day, trading=True):
    windows = (SessionWindow(datetime.combine(day, datetime.min.time()) + timedelta(hours=4),
                             datetime.combine(day, datetime.min.time()) + timedelta(hours=20, minutes=50),
                             "continuous", "main"),) if trading else ()
    return CalendarDay(day, trading, windows, "v1", datetime(2026, 10, 12))


def _setup(tmp_path, days=(date(2026, 10, 13), date(2026, 10, 14)), *, timeframe="15m", weekend=()):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    calendar = TradingCalendar("tbank", "MOEX", tuple(_day(d) for d in days) + tuple(_day(d, False) for d in weekend)) if days else None
    stub = _Calendar(calendar)
    reconciler = MarketDataReconciler(store, source=SOURCE, market_calendar=stub)
    timeline = MultiTimeframeScheduler([timeframe], clock=lambda: datetime(2026, 10, 20, 12))
    cache = MarketDataCache(lambda **kwargs: None, timeline, market_store=store, market_source=SOURCE,
                            market_reconciler=reconciler)
    cache._uids[cache._key(ITEM, timeframe)] = "uid"
    return cache, store, stub


def _frame(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps)})


def _mark(store, timeframe, start, end, state, evidence="test", step=timedelta(minutes=15)):
    stamp = pd.Timestamp(start)
    while stamp < pd.Timestamp(end):
        store.set_interval_state(source=SOURCE, instrument_uid="uid", interval=timeframe, open_time=stamp,
                                 state=state, evidence=evidence)
        stamp += step


def test_scheduled_night_is_closed(tmp_path):
    cache, store, _ = _setup(tmp_path)
    _mark(store, "15m", "2026-10-13 21:00", "2026-10-14 03:45", "scheduled_closed")

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 20:30", "2026-10-13 20:45", "2026-10-14 03:45"))

    assert breaks == {(pd.Timestamp("2026-10-13 20:45"), pd.Timestamp("2026-10-14 03:45")): {"kind": "closed", "missing": 27}}


def test_unmarked_night_with_known_calendar_is_closed_without_write(tmp_path):
    cache, store, stub = _setup(tmp_path)

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 20:45", "2026-10-14 03:45"))

    assert [item["kind"] for item in breaks.values()] == ["closed"]
    assert store.coverage_state(source=SOURCE, instrument_uid="uid", interval="15m", open_time="2026-10-13 23:00") is None
    assert stub.requests == 0


def test_unresolved_interval_inside_session_is_missing(tmp_path):
    cache, store, _ = _setup(tmp_path)
    _mark(store, "15m", "2026-10-13 10:15", "2026-10-13 10:30", "scheduled_closed")
    _mark(store, "15m", "2026-10-13 10:30", "2026-10-13 10:45", "unresolved")

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 10:00", "2026-10-13 10:45"))

    assert list(breaks.values()) == [{"kind": "missing", "missing": 2}]


def test_received_candle_absent_from_frame_is_missing(tmp_path):
    cache, store, _ = _setup(tmp_path)
    store.record_candles(source=SOURCE, instrument_uid="uid", interval="15m", candles=pd.DataFrame({
        "datetime": [pd.Timestamp("2026-10-13 10:15")], "open": [1], "high": [1], "low": [1], "close": [1],
        "volume": [1], "is_complete": [True]}))

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 10:00", "2026-10-13 10:30"))

    assert list(breaks.values()) == [{"kind": "missing", "missing": 1}]


def test_night_before_calendar_is_schedule_unavailable_without_request(tmp_path):
    cache, store, stub = _setup(tmp_path)
    _mark(store, "15m", "2026-10-08 21:00", "2026-10-09 03:45", "unresolved", "schedule_unavailable")

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-08 20:45", "2026-10-09 03:45"))

    assert list(breaks.values()) == [{"kind": "schedule_unavailable", "missing": 27}]
    assert stub.requests == 0


@pytest.mark.parametrize("state", ["confirmed_no_trade", "source_absent"])
def test_resolved_without_candle_is_no_trade(tmp_path, state):
    cache, store, _ = _setup(tmp_path)
    _mark(store, "15m", "2026-10-13 10:15", "2026-10-13 10:30", state)

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 10:00", "2026-10-13 10:30"))

    assert list(breaks.values()) == [{"kind": "no_trade", "missing": 1}]


def test_worst_kind_wins(tmp_path):
    cache, store, _ = _setup(tmp_path)
    _mark(store, "15m", "2026-10-13 21:00", "2026-10-14 03:45", "scheduled_closed")
    _mark(store, "15m", "2026-10-14 04:00", "2026-10-14 04:15", "unresolved")

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 20:45", "2026-10-14 04:15"))

    assert list(breaks.values()) == [{"kind": "missing", "missing": 29}]


def test_cache_without_market_store_marks_every_break_unavailable():
    timeline = MultiTimeframeScheduler(["15m"], clock=lambda: datetime(2026, 10, 20, 12))
    cache = MarketDataCache(lambda **kwargs: None, timeline)

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 10:00", "2026-10-13 10:15", "2026-10-13 11:00"))

    assert breaks == {(pd.Timestamp("2026-10-13 10:15"), pd.Timestamp("2026-10-13 11:00")):
                      {"kind": "schedule_unavailable", "missing": 2}}


def test_store_read_error_marks_breaks_unavailable(tmp_path):
    cache, store, _ = _setup(tmp_path)

    def broken(**kwargs):
        raise OSError("disk")
    store.window_snapshot = broken

    breaks = cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 20:45", "2026-10-14 03:45"))

    assert list(breaks.values()) == [{"kind": "schedule_unavailable", "missing": 27}]


def test_weekend_on_1m_is_closed_with_one_window_read(tmp_path):
    cache, store, _ = _setup(tmp_path, (date(2026, 10, 16), date(2026, 10, 19)), timeframe="1m",
                             weekend=(date(2026, 10, 17), date(2026, 10, 18)))
    calls = []
    original = store.window_snapshot

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    store.window_snapshot = counted

    breaks = cache.chart_breaks(ITEM, "1m", _frame("2026-10-16 20:49", "2026-10-19 04:00"))

    (info,) = breaks.values()
    assert info == {"kind": "closed", "missing": int((pd.Timestamp("2026-10-19 04:00") - pd.Timestamp("2026-10-16 20:50")).total_seconds() // 60)}
    assert len(calls) == 1


def test_contiguous_frame_has_no_breaks(tmp_path):
    cache, _, _ = _setup(tmp_path)

    assert cache.chart_breaks(ITEM, "15m", _frame("2026-10-13 10:00", "2026-10-13 10:15")) == {}
