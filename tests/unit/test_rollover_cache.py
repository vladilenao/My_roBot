from datetime import datetime, timedelta

import pandas as pd

from src.data.cache import MarketDataCache
from src.instruments.model import Instrument
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler


def test_warmup_pairs_do_not_hide_gap_and_calendar_event_is_not_a_bar():
    start = datetime(2026, 1, 1, 10)
    clock = HistoricalClock(start, start+timedelta(days=8), timedelta(minutes=1), 0)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)

    def source(**kw):
        upper = kw.get("end_date") or clock.now()
        lower = kw.get("start_date") or start-timedelta(minutes=4)
        times = pd.date_range(lower, upper, freq="1min", inclusive="left")
        if kw["instrument_id"] == "old":
            times = times[times < start]
        return pd.DataFrame({"datetime": times, "close": 100}), kw["instrument_id"]

    old = Instrument("NG-1.26", "old", "future", "NG-1.26", "old", "NG")
    future = Instrument("NG-2.26", "next", "future", "NG-2.26", "next", "NG")
    cache = MarketDataCache(source, timeline, clock=clock)
    cutoff = start+timedelta(minutes=5)
    cache.set_active_instruments([old], next_event=lambda now: cutoff, active_pairs={("old", "1m")})
    cache.frame_for(old, "1m")
    for _ in range(3):
        clock.advance()
        cache.ensure_loaded(future, "1m")
        assert not cache.has_fresh_closed_bar("1m")
        cache.refresh_if_new_candle("1m", force=True)
        cache.close_tick()
    assert not cache.data_exhausted
    assert cache.take_calendar_event() == cutoff
    assert cache.take_calendar_event() is None
    assert cache.gaps == 0  # Календарное событие не выдано за найденную свечу.
    cache.set_active_instruments([future], active_pairs={("next", "1m")})
    assert cache.has_fresh_closed_bar("1m")  # Завершённый old больше не удерживает гейт.
