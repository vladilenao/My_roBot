from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.bot.run_session import HistoricalRunSession
from src.data.cache import MarketDataCache, TerminalGap
from src.history.preflight import Pair, check_right_boundary
from src.instruments.model import Instrument
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler

START = datetime(2025, 1, 24, 20, 48)
END = datetime(2025, 1, 25, 12)
LAST = datetime(2025, 1, 24, 20, 49)
RIGHT = datetime(2025, 1, 27, 7)


def loader(**kw):
    rows = pd.DataFrame({"datetime": pd.to_datetime([START, LAST, RIGHT]), "close": [100, 103, 1000000]})
    left, right = kw.get("start_date"), kw.get("end_date")
    if left is not None:
        rows = rows.loc[rows["datetime"] >= left]
    if right is not None:
        rows = rows.loc[rows["datetime"] <= right]
    return rows, "uid-1"


def harness(stamps=None):
    provider = SimpleNamespace(base_url="source", snapshot_generation=42)
    clock = HistoricalClock(START, END, timedelta(minutes=1), 0)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    check = check_right_boundary(Pair("NG", "future", "1m", "uid-1", "NG-1.25"),
        start=START, boundary=END, load_candles=loader, client_provider=provider)

    def work(**kw):
        if stamps is None:
            return loader(**kw)
        data, uid = loader(**kw)
        return data.loc[data["datetime"].isin(stamps)], uid

    cache = MarketDataCache(work, timeline, clock=clock, client_provider=provider)
    cache.set_boundary_checks([check])
    session = HistoricalRunSession(clock)
    session.set_boundary_checks([check])
    instrument = Instrument("NG-1.25", "NG", "future", "NG-1.25", "uid-1")
    cache.frame_for(instrument, "1m")
    return clock, cache, session, provider, check


def tick(clock, cache):
    clock.advance()
    cache.refresh_if_new_candle("1m", force=True)
    cache.close_tick()
    cache.note_driver_processed()


def test_terminal_tail_count_is_idempotent_and_excludes_future():
    clock, cache, session, provider, check = harness()
    for _ in range(4):
        tick(clock, cache)
    gap = cache.take_terminal_gap()
    assert gap is not None and session.complete_terminal_gap(gap)
    assert clock.now() == END and session.covered and session.ticks_done() == 2
    assert cache.missed_bars == 910 and cache.gaps == 1
    assert cache.last_processed_bar == LAST
    assert cache.take_terminal_gap() is None
    cache.close_tick()
    assert cache.missed_bars == 910 and cache.gaps == 1
    assert cache.frame_for(Instrument("NG", "NG", "future", uid="uid-1"), "1m")["datetime"].max() == LAST


@pytest.mark.parametrize("failure", ["early", "snapshot", "source", "unconfirmed", "uid"])
def test_proof_cannot_replace_missing_or_changed_runtime_data(failure):
    clock, cache, session, provider, check = harness([START] if failure == "early" else None)
    if failure == "snapshot":
        provider.snapshot_generation = 43
    if failure == "unconfirmed":
        cache.set_boundary_checks([])
    if failure == "uid":
        cache.set_boundary_checks([replace(check, pair=replace(check.pair, instrument_id="other"))])
    for _ in range(2):
        tick(clock, cache)
    if failure == "source":
        original = cache._loader

        def failed(**kw):
            if kw.get("end_date") is not None:
                raise RuntimeError("source unavailable")
            return original(**kw)

        cache._loader = failed
    for _ in range(3):
        tick(clock, cache)
    assert cache.take_terminal_gap() is None
    if cache.data_exhausted:
        session.mark_data_exhausted(cache.data_exhaustion)
    else:
        session.update_data_status(source_failed=cache.history_source_failed,
                                   drivers_complete=cache.final_drivers_processed())
        clock.jump_to(END)
    assert not session.covered


def test_calendar_event_precedes_terminal_completion():
    clock, cache, session, provider, check = harness()
    event = datetime(2025, 1, 25, 0)
    cache.set_active_instruments([Instrument("NG", "NG", "future", uid="uid-1")],
                                 next_event=lambda now: event if now < event else None)
    for _ in range(4):
        tick(clock, cache)
    assert cache.take_terminal_gap() is None
    assert cache.take_calendar_event() == event
    clock.jump_to(event)
    for _ in range(3):
        tick(clock, cache)
    gap = cache.take_terminal_gap()
    assert gap is not None and session.complete_terminal_gap(gap)
    assert cache.gaps == 1 and cache.missed_bars == 910


def test_session_rejects_foreign_identity_even_when_nominal_pair_names_match():
    clock, cache, session, provider, check = harness()
    foreign = replace(check, pair=replace(check.pair, instrument_id="other"))
    assert not session.complete_terminal_gap(TerminalGap(pd.Timestamp(END), pd.Timestamp(LAST), (foreign,)))
    assert clock.now() == START
