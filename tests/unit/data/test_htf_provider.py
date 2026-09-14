from datetime import datetime, timezone

import pandas as pd

from src.data.htf_provider import HtfFrameProvider
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


def _bars(starts):
    starts = [pd.Timestamp(s) for s in starts]
    return pd.DataFrame(
        {
            "datetime": starts,
            "open": [100.0] * len(starts),
            "high": [101.0] * len(starts),
            "low": [99.0] * len(starts),
            "close": [100.5] * len(starts),
            "volume": [1000] * len(starts),
        }
    )


class RecordCache:
    """Кэш, логирующий запросы и отдающий готовые (закрытые) кадры."""

    def __init__(self, frames):
        self.frames = dict(frames)
        self.ensure_calls = []
        self.frame_calls = []

    def ensure_loaded(self, instrument, timeframe):
        self.ensure_calls.append((instrument, timeframe))

    def frame_for(self, instrument, timeframe):
        self.frame_calls.append((instrument, timeframe))
        return self.frames.get(timeframe, pd.DataFrame()).copy()


class FakeLoaderPerTf:
    """Лоадер истории по паре (тикер, таймфрейм) с записью вызовов."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(
        self, ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None, instrument_id=None
    ):
        self.calls.append((ticker, timeframe, start_date))
        rows = [
            b
            for b in self.data.get((ticker, timeframe), [])
            if start_date is None or b > pd.Timestamp(start_date)
        ]
        return _bars(rows), instrument_id or "uid-1"


def _make_provider(frames, clock_at=None):
    clock_at = clock_at or datetime(2024, 1, 1, 9, 0)
    sched = MultiTimeframeScheduler(["5m"], clock=lambda: clock_at)
    return HtfFrameProvider(cache=RecordCache(frames), timeline=sched), sched


class TestHtfFrameProviderSlicing:
    def test_slices_to_max_close(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider(
            {"4h": _bars(["2024-01-01 00:00", "2024-01-01 04:00",
                          "2024-01-01 08:00", "2024-01-01 12:00"])}
        )

        frame = provider.frame_for(inst, "4h", max_close=datetime(2024, 1, 1, 12, 0))

        assert frame["datetime"].tolist() == [
            pd.Timestamp("2024-01-01 00:00"),
            pd.Timestamp("2024-01-01 04:00"),
            pd.Timestamp("2024-01-01 08:00"),
        ]  # бар 12:00 закрывается в 16:00 — позже окна закрытой рабочей свечи

    def test_no_max_close_returns_everything(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider(
            {"4h": _bars(["2024-01-01 00:00", "2024-01-01 04:00", "2024-01-01 08:00"])}
        )

        frame = provider.frame_for(inst, "4h")

        assert len(frame) == 3

    def test_aware_max_close_normalized(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider({"4h": _bars(["2024-01-01 00:00", "2024-01-01 04:00"])})

        frame = provider.frame_for(
            inst, "4h", max_close=datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
        )

        assert frame["datetime"].tolist() == [pd.Timestamp("2024-01-01 00:00")]

    def test_empty_frame_returns_empty(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider({})

        frame = provider.frame_for(inst, "4h", max_close=datetime(2024, 1, 1, 12, 0))

        assert frame.empty

    def test_short_frame_not_raised(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider({"4h": _bars(["2024-01-01 00:00", "2024-01-01 04:00"])})

        frame = provider.frame_for(inst, "4h", min_bars=100)

        assert len(frame) == 2  # провайдер не режет и не падает; о нехватке судит фильтр

    def test_ensure_loaded_delegates_to_cache(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, _ = _make_provider({"4h": _bars(["2024-01-01 00:00"])})

        provider.frame_for(inst, "4h")

        assert provider._cache.ensure_calls == [(inst, "4h")]

    def test_grid_of_inactive_tf_does_not_enter_active_rhythm(self):
        inst = Instrument("SBER", "SBER", "share")
        provider, sched = _make_provider({"4h": _bars(["2024-01-01 00:00"])})

        provider.frame_for(inst, "4h")

        assert sched.timeframes == ("5m",)  # 4h-сетка остаётся ленивой


class TestHtfFrameProviderWithCache:
    def test_full_path_via_cache(self):
        clock = datetime(2024, 1, 1, 8, 15)
        sched = MultiTimeframeScheduler(["5m"], clock=lambda: clock)
        data = {
            ("SBER", "4h"): [pd.Timestamp("2024-01-01 00:00"),
                             pd.Timestamp("2024-01-01 04:00"),
                             pd.Timestamp("2024-01-01 08:00")],
        }
        loader = FakeLoaderPerTf(data)
        from src.data.cache import MarketDataCache

        real_cache = MarketDataCache(loader=loader, timeline=sched)
        provider = HtfFrameProvider(cache=real_cache, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")

        frame = provider.frame_for(inst, "4h", max_close=datetime(2024, 1, 1, 8, 0))

        # 4h загружен по требованию, хотя активный ритм — только 5m
        assert ("SBER", "4h", None) in loader.calls
        # бар 08:00 закрывается в 12:00 — вне окна max_close 08:00
        assert frame["datetime"].tolist() == [
            pd.Timestamp("2024-01-01 00:00"),
            pd.Timestamp("2024-01-01 04:00"),
        ]
        # логирование на cache.FakeRecordCache не нужно: используем реальный кэш
        assert real_cache._frames[("SBER", "share", "4h")] is not None