from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from src.data.cache import CONSECUTIVE_EMPTY_TICKS_BEFORE_END, MarketDataCache
from src.instruments import Instrument
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler

START = datetime(2024, 1, 1, 0, 0)
END = datetime(2024, 1, 1, 1, 0)
INSTRUMENT = Instrument("SBER", "SBER", "share")


def _minutes(count: int, first: str = "2024-01-01 00:00"):
    return [pd.Timestamp(first) + timedelta(minutes=i) for i in range(count)]


def _rows(stamps) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [pd.Timestamp(s) for s in stamps],
            "open": [100.0] * len(stamps),
            "high": [101.0] * len(stamps),
            "low": [99.0] * len(stamps),
            "close": [100.5] * len(stamps),
            "volume": [1000] * len(stamps),
        }
    )


class RecordingLoader:
    """Отдаёт заранее заданные бары и считает попытки дозагрузки."""

    def __init__(self, data, error=None):
        self.data = data
        self.error = error
        self.calls = []
        self.sources = []

    def __call__(
        self,
        ticker,
        instrument_type,
        timeframe,
        start_date=None,
        end_date=None,
        token=None,
        instrument_id=None,
        client_provider=None,
        clock=None,
    ):
        self.calls.append(start_date)
        self.sources.append((client_provider, clock))
        if self.error is not None:
            raise self.error
        rows = [b for b in self.data if start_date is None or b > pd.Timestamp(start_date)]
        return _rows(rows), "uid-1"


class HistoryHarness:
    """Кэш на виртуальных часах: шаг 1m, диапазон 00:00 … 01:00."""

    def __init__(self, bars, timeframes=("1m",), **kw):
        self.clock = HistoricalClock(
            START, END, timedelta(minutes=1), 0.0, sleeper=lambda secs: None
        )
        self.timeline = MultiTimeframeScheduler(list(timeframes), clock=self.clock)
        self.loader = RecordingLoader(bars)
        self.cache = MarketDataCache(
            loader=self.loader, timeline=self.timeline, clock=self.clock, **kw
        )
        for timeframe in timeframes:
            self.cache.frame_for(INSTRUMENT, timeframe)

    def tick(self, timeframes=None):
        """Один рыночный тик целиком: все ТФ, затем закрытие тика — как в бою."""
        self.clock.advance()
        for timeframe in timeframes or self.timeline.timeframes:
            self.cache.refresh_if_new_candle(timeframe)
        self.cache.close_tick()
        return self.cache

    def ticks(self, count: int, timeframes=None):
        for _ in range(count):
            self.tick(timeframes)
        return self.cache


class TestHistorySingleAttempt:
    def test_one_load_per_boundary(self):
        harness = HistoryHarness(_minutes(10))
        initial = len(harness.loader.calls)

        harness.ticks(3)

        assert len(harness.loader.calls) == initial + 3

    def test_no_throttle_between_loads(self):
        harness = HistoryHarness(_minutes(10), data_refresh_min_interval=300)
        sleeps = []

        with patch("src.data.cache.time.sleep", sleeps.append):
            harness.ticks(3)

        assert sleeps == []

    def test_force_reload_ignored_when_exhausted(self):
        harness = HistoryHarness(_minutes(2))
        harness.ticks(1)
        harness.cache.data_exhausted = True
        before = len(harness.loader.calls)

        harness.cache.refresh_if_new_candle("1m", force=True)

        assert len(harness.loader.calls) == before

    def test_rate_limit_surfaces_without_pause(self):
        harness = HistoryHarness(_minutes(10))
        harness.loader.error = RuntimeError("resource_exhausted")
        before = len(harness.loader.calls)

        with pytest.raises(RuntimeError):
            harness.ticks(1)

        assert harness.cache._retry_after is None
        assert len(harness.loader.calls) == before + 1


class TestHistoryGapCounting:
    def test_missing_bar_counts_as_gap(self):
        bars = [b for b in _minutes(20) if b != pd.Timestamp("2024-01-01 00:05")]
        harness = HistoryHarness(bars)

        cache = harness.ticks(10)

        assert cache.missed_bars == 1
        assert cache.data_exhausted is False

    def test_run_continues_after_gap(self):
        bars = [b for b in _minutes(20) if b != pd.Timestamp("2024-01-01 00:05")]
        harness = HistoryHarness(bars)

        cache = harness.ticks(15)

        assert cache.missed_bars == 1
        assert cache.data_exhausted is False
        assert pd.Timestamp("2024-01-01 00:14") in list(
            cache.frame_for(INSTRUMENT, "1m")["datetime"]
        )

    def test_gap_counter_starts_at_zero(self):
        assert HistoryHarness(_minutes(5)).cache.missed_bars == 0


class TestHistoryDataEnd:
    def test_end_declared_after_consecutive_empty_ticks(self):
        harness = HistoryHarness(_minutes(10))

        cache = harness.ticks(10 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is True

    def test_single_empty_tick_is_not_the_end(self):
        harness = HistoryHarness(_minutes(10))

        cache = harness.ticks(1)

        assert cache.data_exhausted is False

    def test_empty_ticks_reset_after_progress(self):
        bars = [b for b in _minutes(20) if b != pd.Timestamp("2024-01-01 00:05")]
        harness = HistoryHarness(bars)

        harness.ticks(6)

        assert harness.cache._empty_ticks == 1
        assert harness.cache.missed_bars == 1

        cache = harness.ticks(1)

        assert cache._empty_ticks == 0
        assert cache.data_exhausted is False

    def test_end_declared_at_data_boundary(self):
        harness = HistoryHarness(_minutes(10))

        cache = harness.ticks(10)

        assert cache.data_exhausted is False
        assert cache.missed_bars == 0

    def test_loaded_bars_still_available_after_end(self):
        harness = HistoryHarness(_minutes(5))

        cache = harness.ticks(20)

        assert len(cache.frame_for(INSTRUMENT, "1m")) == 5


class TestHistoryMultiTimeframeTick:
    """Тик считается один раз, а не по разу на каждый таймфрейм."""

    def test_slower_timeframes_do_not_end_the_run(self):
        """Старшие ТФ, у которых не было нового бара, не объявляют конец данных."""
        harness = HistoryHarness(_minutes(10), timeframes=("1m", "5m", "15m", "1h"))

        cache = harness.ticks(5)

        assert cache.data_exhausted is False
        assert cache._empty_ticks == 0

    def test_run_processes_every_minute_of_the_range(self):
        harness = HistoryHarness(_minutes(10), timeframes=("1m", "5m", "1h"))

        cache = harness.ticks(5)

        assert cache.missed_bars == 0
        assert harness.clock.now() == pd.Timestamp("2024-01-01 00:05")

    def test_end_is_still_declared_when_driver_stops(self):
        """Когда перестаёт приходить ведущий ТФ, конец данных объявляется."""
        harness = HistoryHarness(_minutes(10), timeframes=("1m", "5m", "1h"))

        cache = harness.ticks(10 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is True

    def test_gap_counted_once_per_tick_not_per_timeframe(self):
        bars = [b for b in _minutes(20) if b != pd.Timestamp("2024-01-01 00:05")]
        harness = HistoryHarness(bars, timeframes=("1m", "5m", "1h"))

        cache = harness.ticks(10)

        assert cache.missed_bars == 1
        assert cache.data_exhausted is False

    def test_repeated_force_call_for_one_timeframe_not_double_counted(self):
        harness = HistoryHarness(_minutes(10), timeframes=("1m",))

        harness.clock.advance()
        harness.cache.refresh_if_new_candle("1m", force=True)
        harness.cache.refresh_if_new_candle("1m", force=True)
        harness.cache.close_tick()

        assert harness.cache.missed_bars == 0
        assert harness.cache._empty_ticks == 0

    def test_tick_without_expected_bar_is_not_counted_empty(self):
        """ТФ без сдвинувшейся границы ничего не ждёт и не влияет на счётчик."""
        harness = HistoryHarness(_minutes(10), timeframes=("1m", "1h"))

        cache = harness.ticks(1, timeframes=("1h",))

        assert cache._empty_ticks == 0
        assert cache.data_exhausted is False


class TestLiveBranchUnchanged:
    def _live(self, bars, **kw):
        timeline = MultiTimeframeScheduler(["1h"], clock=lambda: START)
        loader = RecordingLoader(bars)
        return MarketDataCache(loader=loader, timeline=timeline, **kw), loader

    def test_default_cache_is_live(self):
        cache, _ = self._live(_minutes(3))

        assert cache.data_exhausted is False
        assert cache.missed_bars == 0

    def test_live_loads_do_not_receive_source_arguments(self):
        cache, loader = self._live(_minutes(3))
        cache.frame_for(INSTRUMENT, "1h")
        cache.refresh_if_new_candle("1h", force=True)

        assert all(source == (None, None) for source in loader.sources)


    def test_force_reload_kept_for_live(self):
        cache, loader = self._live(_minutes(3))
        cache.frame_for(INSTRUMENT, "1h")
        before = len(loader.calls)

        cache.refresh_if_new_candle("1h", force=True)

        assert len(loader.calls) > before

    def test_throttle_still_applies_in_live(self):
        timeline = MultiTimeframeScheduler(["1h"], clock=lambda: START)
        loader = RecordingLoader(_minutes(3))
        cache = MarketDataCache(
            loader=loader, timeline=timeline, data_refresh_min_interval=300
        )
        cache.frame_for(INSTRUMENT, "1h")
        before = len(loader.calls)

        cache.refresh_if_new_candle("1h", force=True)

        assert len(loader.calls) == before

    def test_rate_limit_pauses_live_reload(self):
        timeline = MultiTimeframeScheduler(["1h"], clock=lambda: START)
        loader = RecordingLoader(_minutes(3))
        cache = MarketDataCache(loader=loader, timeline=timeline)
        cache.frame_for(INSTRUMENT, "1h")
        loader.error = RuntimeError("resource_exhausted")

        cache.refresh_if_new_candle("1h", force=True)

        assert cache._retry_after is not None
class TestHistoryDataSource:
    def test_history_passes_provider_and_virtual_clock(self):
        provider = object()
        harness = HistoryHarness(_minutes(10), client_provider=provider)

        assert all(given is provider for given, _ in harness.loader.sources)
        assert all(given is harness.clock for _, given in harness.loader.sources)

    def test_live_loads_receive_neither_provider_nor_clock(self):
        timeline = MultiTimeframeScheduler(["1h"], clock=lambda: START)
        loader = RecordingLoader(_minutes(3))
        cache = MarketDataCache(loader=loader, timeline=timeline)
        cache.frame_for(INSTRUMENT, "1h")

        assert all(source == (None, None) for source in loader.sources)
