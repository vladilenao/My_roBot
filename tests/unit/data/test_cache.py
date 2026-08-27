from datetime import datetime, timezone

import pandas as pd

from src.data.cache import MarketDataCache
from src.instruments import Instrument
from src.scheduler.timing import CandleScheduler


def _bars(times):
    dt = [pd.Timestamp(f"2024-01-01 {t}") for t in times]
    return pd.DataFrame({
        "datetime": dt,
        "open": [100.0] * len(dt),
        "high": [101.0] * len(dt),
        "low": [99.0] * len(dt),
        "close": [100.5] * len(dt),
        "volume": [1000] * len(dt),
    })


class FakeLoader:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None):
        self.calls.append(start_date)
        rows = [
            b
            for b in self.data[ticker]
            if start_date is None or b > pd.Timestamp(start_date)
        ]
        return _bars_from_rows(rows), "uid-1"


def _bars_from_rows(rows):
    return pd.DataFrame({
        "datetime": [pd.Timestamp(r) for r in rows],
        "open": [100.0] * len(rows),
        "high": [101.0] * len(rows),
        "low": [99.0] * len(rows),
        "close": [100.5] * len(rows),
        "volume": [1000] * len(rows),
    })


class TestMarketDataCache:
    def _make(self, data, clock_at):
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock_at[0])
        cache = MarketDataCache(loader=FakeLoader(data), timeline=sched)
        return cache

    def test_first_load_returns_closed_candles(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["08:00", "09:00", "10:00"])  # 10:00 — живая (незакрытая)
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")

        frame = cache.frame_for(inst)

        assert list(frame["datetime"]) == [
            pd.Timestamp("2024-01-01 08:00"),
            pd.Timestamp("2024-01-01 09:00"),
        ]

    def test_no_new_bar_no_reload(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["08:00", "09:00", "10:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst)
        loader = cache._loader
        initial_calls = len(loader.calls)

        cache.refresh_if_new_candle()  # граница не сместилась

        assert len(loader.calls) == initial_calls

    def test_new_bar_triggers_incremental_load(self):
        clock = [datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)]
        candles = [pd.Timestamp("2024-01-01 06:00"),
                   pd.Timestamp("2024-01-01 07:00"),
                   pd.Timestamp("2024-01-01 08:00"),
                   pd.Timestamp("2024-01-01 09:00")]
        loader = FakeLoader({"SBER": candles})
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst)  # 09:00 — живая, кэш до 08:00

        assert len(loader.calls) == 1

        # появился новый закрытый бар 10:00, часы перешли на 10:00
        loader.data["SBER"] = candles + [pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

        cache.refresh_if_new_candle()

        assert len(loader.calls) == 2
        assert loader.calls[-1] == pd.Timestamp("2024-01-01 08:00")  # дозагрузка с последнего бара
        frame = cache.frame_for(inst)
        assert frame["datetime"].max() == pd.Timestamp("2024-01-01 09:00")
        assert len(frame) == 4

    def test_live_candle_excluded(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["09:00", "10:00", "11:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")

        frame = cache.frame_for(inst)

        assert list(frame["datetime"]) == [pd.Timestamp("2024-01-01 09:00")]

    def test_boundaries_and_loaded_are_naive(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        candles = [
            pd.Timestamp("2024-01-01 06:00"),
            pd.Timestamp("2024-01-01 07:00"),
            pd.Timestamp("2024-01-01 08:00"),
            pd.Timestamp("2024-01-01 09:00"),
        ]
        loader = FakeLoader({"SBER": candles})
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst)

        assert cache._observed[("SBER", "share")].tzinfo is None
        assert cache._last_loaded[("SBER", "share")].tzinfo is None

        # новый закрытый бар 10:00, часы перешли (naive) — дозагрузка не падает
        loader.data["SBER"] = candles + [pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 10, 0)
        cache.refresh_if_new_candle()

        assert cache._last_loaded[("SBER", "share")].tzinfo is None
        assert cache._last_loaded[("SBER", "share")] == pd.Timestamp("2024-01-01 09:00")



