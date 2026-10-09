from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from src.data.cache import MarketDataCache
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


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
        self.instrument_ids = []

    def __call__(
        self, ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None, instrument_id=None
    ):
        self.calls.append(start_date)
        self.instrument_ids.append(instrument_id)
        rows = [
            b
            for b in self.data[ticker]
            if start_date is None or b > pd.Timestamp(start_date)
        ]
        return _bars_from_rows(rows), instrument_id or "uid-1"


def _bars_from_rows(rows):
    return pd.DataFrame({
        "datetime": [pd.Timestamp(r) for r in rows],
        "open": [100.0] * len(rows),
        "high": [101.0] * len(rows),
        "low": [99.0] * len(rows),
        "close": [100.5] * len(rows),
        "volume": [1000] * len(rows),
    })


class FlakyLoader(FakeLoader):
    """Лоадер, бросающий rate-limit на заданном вызове (по счётчику вызовов)."""

    def __init__(self, data, fail_on_call, error):
        super().__init__(data)
        self.fail_on_call = fail_on_call
        self.error = error

    def __call__(self, ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None, instrument_id=None):
        if len(self.calls) == self.fail_on_call - 1:
            self.calls.append(start_date)
            self.instrument_ids.append(instrument_id)
            raise RuntimeError(self.error)
        return super().__call__(ticker, instrument_type, timeframe, start_date, end_date, token, instrument_id)


class TestMarketDataCache:
    def _make(self, data, clock_at):
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock_at[0])
        cache = MarketDataCache(loader=FakeLoader(data), timeline=sched)
        return cache

    def test_first_load_returns_closed_candles(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["08:00", "09:00", "10:00"])  # 10:00 — живая (незакрытая)
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")

        frame = cache.frame_for(inst, "1h")

        assert list(frame["datetime"]) == [
            pd.Timestamp("2024-01-01 08:00"),
            pd.Timestamp("2024-01-01 09:00"),
        ]

    def test_no_new_bar_no_reload(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["08:00", "09:00", "10:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")
        loader = cache._loader
        initial_calls = len(loader.calls)

        cache.refresh_if_new_candle("1h")  # граница не сместилась

        assert len(loader.calls) == initial_calls

    def test_new_bar_triggers_incremental_load(self):
        clock = [datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)]
        candles = [pd.Timestamp("2024-01-01 06:00"),
                   pd.Timestamp("2024-01-01 07:00"),
                   pd.Timestamp("2024-01-01 08:00"),
                   pd.Timestamp("2024-01-01 09:00")]
        loader = FakeLoader({"SBER": candles})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")  # 09:00 — живая, кэш до 08:00

        assert len(loader.calls) == 1

        # появился новый закрытый бар 10:00, часы перешли на 10:00
        loader.data["SBER"] = candles + [pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

        cache.refresh_if_new_candle("1h")

        assert len(loader.calls) == 2
        assert loader.calls[-1] == pd.Timestamp("2024-01-01 08:00")  # дозагрузка с последнего бара
        frame = cache.frame_for(inst, "1h")
        assert frame["datetime"].max() == pd.Timestamp("2024-01-01 09:00")
        assert len(frame) == 4

    def test_live_candle_excluded(self):
        clock = [datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)]
        bars = _bars(["09:00", "10:00", "11:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")

        frame = cache.frame_for(inst, "1h")

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
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        assert cache._observed[("SBER", "share", "1h")].tzinfo is None
        assert cache._last_loaded[("SBER", "share", "1h")].tzinfo is None

        # новый закрытый бар 10:00, часы перешли (naive) — дозагрузка не падает
        loader.data["SBER"] = candles + [pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 10, 0)
        cache.refresh_if_new_candle("1h")

        assert cache._last_loaded[("SBER", "share", "1h")].tzinfo is None
        assert cache._last_loaded[("SBER", "share", "1h")] == pd.Timestamp("2024-01-01 09:00")

    def test_has_fresh_closed_bar_true_when_present(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        bars = _bars(["06:00", "07:00", "08:00", "09:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        assert cache.has_fresh_closed_bar("1h") is True

    def test_has_fresh_closed_bar_false_when_missing(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        bars = _bars(["06:00", "07:00", "08:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        assert cache.has_fresh_closed_bar("1h") is False

    def test_has_fresh_closed_bar_true_when_nothing_loaded(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        bars = _bars(["06:00"])
        cache = self._make({"SBER": bars["datetime"].tolist()}, clock)

        assert cache.has_fresh_closed_bar("1h") is True

    def test_force_refresh_pulls_late_bar(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        loader = FakeLoader({"SBER": [pd.Timestamp("2024-01-01 06:00"),
                                      pd.Timestamp("2024-01-01 07:00"),
                                      pd.Timestamp("2024-01-01 08:00")]})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        assert cache.has_fresh_closed_bar("1h") is False

        # свежий закрытый бар 09:00 публикуется с задержкой
        loader.data["SBER"] = [pd.Timestamp("2024-01-01 06:00"),
                               pd.Timestamp("2024-01-01 07:00"),
                               pd.Timestamp("2024-01-01 08:00"),
                               pd.Timestamp("2024-01-01 09:00")]

        cache.refresh_if_new_candle("1h", force=True)

        assert cache.has_fresh_closed_bar("1h") is True
        assert cache.frame_for(inst, "1h")["datetime"].max() == pd.Timestamp("2024-01-01 09:00")

    def test_refresh_without_force_skips_after_observed(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        loader = FakeLoader({"SBER": [pd.Timestamp("2024-01-01 06:00"),
                                      pd.Timestamp("2024-01-01 07:00"),
                                      pd.Timestamp("2024-01-01 08:00")]})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")
        cache.refresh_if_new_candle("1h")
        calls_before = len(loader.calls)

        # без force граница уже отслежена — повторной загрузки нет
        loader.data["SBER"] = [pd.Timestamp("2024-01-01 06:00"),
                               pd.Timestamp("2024-01-01 07:00"),
                               pd.Timestamp("2024-01-01 08:00"),
                               pd.Timestamp("2024-01-01 09:00")]
        cache.refresh_if_new_candle("1h")

        assert len(loader.calls) == calls_before
        assert cache.has_fresh_closed_bar("1h") is False

    def test_reload_reuses_cached_instrument_id(self):
        clock = [datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)]
        loader = FakeLoader({"SBER": [pd.Timestamp("2024-01-01 06:00"),
                                      pd.Timestamp("2024-01-01 07:00"),
                                      pd.Timestamp("2024-01-01 08:00"),
                                      pd.Timestamp("2024-01-01 09:00")]})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        assert loader.instrument_ids == [None]  # первый вызов — UID ещё не известен

        # появился новый закрытый бар 10:00, часы перешли — инкрементальная дозагрузка
        loader.data["SBER"] = [pd.Timestamp("2024-01-01 06:00"),
                               pd.Timestamp("2024-01-01 07:00"),
                               pd.Timestamp("2024-01-01 08:00"),
                               pd.Timestamp("2024-01-01 09:00"),
                               pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        cache.refresh_if_new_candle("1h")

        assert loader.instrument_ids == [None, "uid-1"]  # повторная загрузка переиспользует UID

    def test_force_reload_throttled_within_interval(self):
        clock = [datetime(2024, 1, 1, 9, 0)]
        loader = FakeLoader({"SBER": [pd.Timestamp("2024-01-01 06:00"),
                                      pd.Timestamp("2024-01-01 07:00"),
                                      pd.Timestamp("2024-01-01 08:00"),
                                      pd.Timestamp("2024-01-01 09:00")]})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=300)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")
        assert len(loader.calls) == 1

        loader.data["SBER"] = [pd.Timestamp("2024-01-01 06:00"),
                               pd.Timestamp("2024-01-01 07:00"),
                               pd.Timestamp("2024-01-01 08:00"),
                               pd.Timestamp("2024-01-01 09:00"),
                               pd.Timestamp("2024-01-01 10:00")]
        clock[0] = datetime(2024, 1, 1, 9, 3)  # 3 мин с прошлого API < 300с — форс дозагрузку тормозит
        cache.refresh_if_new_candle("1h", force=True)
        assert len(loader.calls) == 1

        clock[0] = datetime(2024, 1, 1, 9, 8)  # 8 мин >= 300с — интервал истёк, дозагрузка выполняется
        cache.refresh_if_new_candle("1h", force=True)
        assert len(loader.calls) == 2
        assert cache.has_fresh_closed_bar("1h") is True

    def test_bounded_backfill_window_bounds_incremental_start(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        loader = FakeLoader({"SBER": [pd.Timestamp("2024-01-01 06:00"),
                                      pd.Timestamp("2024-01-01 07:00"),
                                      pd.Timestamp("2024-01-01 08:00"),
                                      pd.Timestamp("2024-01-01 09:00"),
                                      pd.Timestamp("2024-01-01 10:00")]})
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_backfill_window_seconds=3600)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        loader.data["SBER"].append(pd.Timestamp("2024-01-01 11:00"))
        clock[0] = datetime(2024, 1, 1, 11, 30)
        cache.refresh_if_new_candle("1h")

        start = loader.calls[-1]
        assert start == pd.Timestamp("2024-01-01 10:30")  # max(09:00, 11:30-1h)
        assert pd.Timestamp("2024-01-01 11:30") - start <= pd.Timedelta(seconds=3600)

    def test_rate_limit_pauses_reload_and_resumes_after(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        loader = FlakyLoader(
            {"SBER": [pd.Timestamp("2024-01-01 06:00"),
                      pd.Timestamp("2024-01-01 07:00"),
                      pd.Timestamp("2024-01-01 08:00"),
                      pd.Timestamp("2024-01-01 09:00")]},
            fail_on_call=2,
            error="RESOURCE_EXHAUSTED ratelimit_reset=30",
        )
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=5)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "1h")

        clock[0] = datetime(2024, 1, 1, 11, 5)
        loader.data["SBER"] = [pd.Timestamp("2024-01-01 06:00"),
                               pd.Timestamp("2024-01-01 07:00"),
                               pd.Timestamp("2024-01-01 08:00"),
                               pd.Timestamp("2024-01-01 09:00"),
                               pd.Timestamp("2024-01-01 10:00"),
                               pd.Timestamp("2024-01-01 11:00")]
        cache.refresh_if_new_candle("1h")
        assert len(loader.calls) == 2  # неуспешная попытка зафиксирована
        assert cache._retry_after is not None

        clock[0] = datetime(2024, 1, 1, 11, 5, 10)  # 10с после сбоя < 30с — пауза не истекла
        cache.refresh_if_new_candle("1h")
        assert len(loader.calls) == 2  # API не дёргался

        clock[0] = datetime(2024, 1, 1, 11, 6, 0)  # 60с >= 30с — дозагрузка возобновляется
        cache.refresh_if_new_candle("1h")
        assert len(loader.calls) == 3
        assert cache.has_fresh_closed_bar("1h") is True





class FakeLoaderPerTf:
    """Лоадер с данными по паре (тикер, таймфрейм)."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(
        self, ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None, instrument_id=None
    ):
        self.calls.append((ticker, timeframe))
        rows = [
            b
            for b in self.data.get((ticker, timeframe), [])
            if start_date is None or b > pd.Timestamp(start_date)
        ]
        return _bars_from_rows(rows), instrument_id or "uid-1"


class TestMultiTimeframeCache:
    def _make(self, data, clock_at):
        sched = MultiTimeframeScheduler(["15m", "1h"], clock=lambda: clock_at[0])
        return MarketDataCache(loader=FakeLoaderPerTf(data), timeline=sched)

    def test_frames_of_different_timeframes_are_independent(self):
        clock = [datetime(2024, 1, 1, 10, 45)]
        data = {
            ("SBER", "15m"): [pd.Timestamp(f"2024-01-01 09:{m}") for m in (0, 15, 30, 45)]
                             + [pd.Timestamp("2024-01-01 10:00"), pd.Timestamp("2024-01-01 10:15")],
            ("SBER", "1h"): [pd.Timestamp("2024-01-01 08:00"), pd.Timestamp("2024-01-01 09:00")],
        }
        cache = self._make(data, clock)
        inst = Instrument("SBER", "SBER", "share")

        frame_15m = cache.frame_for(inst, "15m")
        frame_1h = cache.frame_for(inst, "1h")

        assert len(frame_15m) == 6  # все 15m-бары закрыты (живая 10:45 не загружалась)
        assert list(frame_1h["datetime"]) == [
            pd.Timestamp("2024-01-01 08:00"),
            pd.Timestamp("2024-01-01 09:00"),
        ]  # обе 1h-свечи закрыты к 10:45
        # кадры разных ТФ — разные сетки: 1h выровнены по часу, 15m содержит четверти
        assert all(t.minute == 0 for t in frame_1h["datetime"])
        assert pd.Timestamp("2024-01-01 09:15") in set(frame_15m["datetime"])
        assert pd.Timestamp("2024-01-01 09:15") not in set(frame_1h["datetime"])

    def test_freshness_tracked_per_timeframe(self):
        clock = [datetime(2024, 1, 1, 10, 45)]
        data = {
            # 15m: свежий закрытый бар 10:30 есть (ожидаемый previous_start)
            ("SBER", "15m"): [pd.Timestamp("2024-01-01 10:00"), pd.Timestamp("2024-01-01 10:15"),
                              pd.Timestamp("2024-01-01 10:30")],
            # 1h: последний закрытый бар 08:00, ожидаемый 09:00 отсутствует
            ("SBER", "1h"): [pd.Timestamp("2024-01-01 07:00"), pd.Timestamp("2024-01-01 08:00")],
        }
        cache = self._make(data, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.frame_for(inst, "15m")
        cache.frame_for(inst, "1h")

        assert cache.has_fresh_closed_bar("15m") is True
        assert cache.has_fresh_closed_bar("1h") is False

    def test_force_fairly_refreshes_all_frames_across_windows(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        data = {
            ("BR", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8)],
            ("ED", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8)],
            ("SBER", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8)],
        }
        loader = FakeLoaderPerTf(data)
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=300)
        for ticker in ("BR", "ED", "SBER"):
            cache._last_api_attempt = pd.Timestamp("2020-01-01")  # не спать в _initial_load между кадрами
            cache.frame_for(Instrument(ticker, ticker, "share"), "1h")
        assert len(loader.calls) == 3

        # свежие закрытые бары 09:00 публикуются с задержкой у всех пар
        for ticker in ("BR", "ED", "SBER"):
            data[(ticker, "1h")].append(pd.Timestamp("2024-01-01 09:00"))
        assert cache.has_fresh_closed_bar("1h") is False

        # одно окно (>= интервала) — одна дозагрузка; готовность — не более чем за число кадров окон
        windows = 0
        while not cache.has_fresh_closed_bar("1h") and windows < 6:
            clock[0] += timedelta(seconds=301)
            cache.refresh_if_new_candle("1h", force=True)
            windows += 1

        assert windows == 3  # не больше числа кадров ТФ
        assert cache.has_fresh_closed_bar("1h") is True
        assert [c[0] for c in loader.calls[3:]] == ["BR", "ED", "SBER"]  # детерминированный порядок
        for ticker in ("BR", "ED", "SBER"):
            frame = cache.frame_for(Instrument(ticker, ticker, "share"), "1h")
            assert frame["datetime"].max() == pd.Timestamp("2024-01-01 09:00")

    def test_throttled_frame_not_marked_observed(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        data = {
            ("BR", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8, 9)],
            ("SBER", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8, 9)],
        }
        loader = FakeLoaderPerTf(data)
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=7200)
        for ticker in ("BR", "SBER"):
            cache._last_api_attempt = pd.Timestamp("2020-01-01")  # не спать в _initial_load между кадрами
            cache.frame_for(Instrument(ticker, ticker, "share"), "1h")

        observed_before = {
            ticker: cache._observed[(ticker, "share", "1h")]
            for ticker in ("BR", "SBER")
        }
        clock[0] = datetime(2024, 1, 1, 11, 2)  # граница 11:00, но 62 мин < 7200с — окно закрыто
        cache.refresh_if_new_candle("1h", force=True)

        assert len(loader.calls) == 2  # API не дёргался
        assert all(cache._observed[(t, "share", "1h")] == observed_before[t] for t in ("BR", "SBER"))

        # окно открылось — дозагружается самый отстающий кадр за один вызов
        clock[0] = datetime(2024, 1, 1, 12, 30)
        cache.refresh_if_new_candle("1h", force=True)
        assert len(loader.calls) == 3
        assert loader.calls[-1] == ("BR", "1h")  # BR отсортирован первым
        assert cache._observed[("SBER", "share", "1h")] == observed_before["SBER"]

    def test_refresh_prioritizes_most_stale_frame(self):
        clock = [datetime(2024, 1, 1, 10, 0)]
        data = {
            # SBER уже обладает свежим закрытым баром 09:00
            ("SBER", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8, 9)],
            # BR отстаёт: бар 09:00 в данных так и не появился
            ("BR", "1h"): [pd.Timestamp(f"2024-01-01 0{i}:00") for i in (6, 7, 8)],
        }
        loader = FakeLoaderPerTf(data)
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=300)
        cache._last_api_attempt = pd.Timestamp("2020-01-01")  # не спать в _initial_load между кадрами
        cache.frame_for(Instrument("SBER", "SBER", "share"), "1h")
        cache._last_api_attempt = pd.Timestamp("2020-01-01")
        cache.frame_for(Instrument("BR", "BR", "share"), "1h")
        assert cache.has_fresh_closed_bar("1h") is False  # BR держит гейт

        for _ in range(5):
            clock[0] += timedelta(seconds=301)
            cache.refresh_if_new_candle("1h", force=True)

        # свежий кадр SBER не перезапрашивается вхолостую — лимит уходит отстающему BR
        assert [c for c in loader.calls if c[0] == "SBER"] == [("SBER", "1h")]  # только первичная загрузка
        assert cache.has_fresh_closed_bar("1h") is False

        # BR получил поздний бар 09:00 — следующее окно закрывает гейт
        data[("BR", "1h")].append(pd.Timestamp("2024-01-01 09:00"))
        clock[0] += timedelta(seconds=301)
        cache.refresh_if_new_candle("1h", force=True)
        assert cache.has_fresh_closed_bar("1h") is True


class FakeLoaderWithStart:
    """Лоадер по паре (тикер, таймфрейм) с записью start_date дозагрузок."""

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
        return _bars_from_rows(rows), instrument_id or "uid-2"


class TestEnsureLoaded:
    def _make(self, data, clock_at):
        sched = MultiTimeframeScheduler(["1h"], clock=lambda: clock_at[0])
        cache = MarketDataCache(loader=FakeLoaderWithStart(data), timeline=sched)
        return cache, sched

    def test_loads_inactive_timeframe_on_demand(self):
        clock = [datetime(2024, 1, 1, 8, 15)]
        data = {
            ("SBER", "4h"): [pd.Timestamp("2024-01-01 00:00"),
                             pd.Timestamp("2024-01-01 04:00"),
                             pd.Timestamp("2024-01-01 08:00")],
        }
        cache, sched = self._make(data, clock)
        inst = Instrument("SBER", "SBER", "share")

        cache.ensure_loaded(inst, "4h")

        # 4h вне активного ритма, но запрошен — загружен целиком
        assert ("SBER", "4h", None) in cache._loader.calls
        assert len(cache._frames[("SBER", "share", "4h")]) == 3
        assert sched.timeframes == ("1h",)  # ленивая 4h-сетка не входит в ритм

    def test_incremental_reload_on_existing_frame(self):
        clock = [datetime(2024, 1, 1, 8, 15)]
        data = {
            ("SBER", "4h"): [pd.Timestamp("2024-01-01 00:00"),
                             pd.Timestamp("2024-01-01 04:00")],
        }
        cache, _ = self._make(data, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.ensure_loaded(inst, "4h")
        calls_before = list(cache._loader.calls)

        # появился новый закрытый 4h-бар
        data[("SBER", "4h")].append(pd.Timestamp("2024-01-01 08:00"))
        cache.ensure_loaded(inst, "4h")

        # дозагрузка идёт с последнего известного бара 04:00
        assert cache._loader.calls == calls_before + [("SBER", "4h", pd.Timestamp("2024-01-01 04:00"))]
        raw = cache._frames[("SBER", "share", "4h")]
        assert raw["datetime"].max() == pd.Timestamp("2024-01-01 08:00")

    def test_frame_for_returns_closed_only(self):
        clock = [datetime(2024, 1, 1, 8, 15)]
        data = {
            ("SBER", "4h"): [pd.Timestamp("2024-01-01 00:00"),
                             pd.Timestamp("2024-01-01 04:00"),
                             pd.Timestamp("2024-01-01 08:00")],
        }
        cache, _ = self._make(data, clock)
        inst = Instrument("SBER", "SBER", "share")
        cache.ensure_loaded(inst, "4h")

        frame = cache.frame_for(inst, "4h")

        # бар 08:00 закрывается в 12:00 — ещё живой, наружу не выдаётся
        assert frame["datetime"].tolist() == [
            pd.Timestamp("2024-01-01 00:00"),
            pd.Timestamp("2024-01-01 04:00"),
        ]


class TestFreshnessTolerance:
    """Терпимый гейт готовности кадра: freshness_tolerance_bars (догон бар-публикации)."""

    def _make(self, clock, tolerance):
        sched = MultiTimeframeScheduler(["1m", "1h"], clock=lambda: clock[0])
        cache = MarketDataCache(loader=FakeLoader({}), timeline=sched, freshness_tolerance_bars=tolerance)
        return cache

    @staticmethod
    def _seed(cache, ticker, tf, times):
        cache._frames[(ticker, "future", tf)] = _bars(times)

    def test_lagging_pair_within_tolerance_still_blocks(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 2)  # now 10:20, expected 10:19, tol = 2 бара = 120с
        self._seed(cache, "NG", "1m", ["10:18"])  # в допуске, но отстаёт от ожидаемого
        self._seed(cache, "SI", "1m", ["10:19"])  # в норме

        assert cache.has_fresh_closed_bar("1m") is False

    def test_lagging_pair_over_tolerance_excluded(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 2)
        self._seed(cache, "NG", "1m", ["10:16"])  # старше threshold 10:17 — неликвид, исключён
        self._seed(cache, "SI", "1m", ["10:19"])  # в норме

        assert cache.has_fresh_closed_bar("1m") is True

    def test_all_pairs_stale_no_fresh_bars(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 2)
        self._seed(cache, "NG", "1m", ["10:16"])
        self._seed(cache, "SI", "1m", ["10:15"])

        assert cache.has_fresh_closed_bar("1m") is False

    def test_no_frames_returns_true(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 2)

        assert cache.has_fresh_closed_bar("1m") is True

    def test_tolerance_applies_per_timeframe(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 2)
        self._seed(cache, "NG", "1m", ["10:16"])  # вне допуска по 1m
        self._seed(cache, "SI", "1m", ["10:19"])
        self._seed(cache, "NG", "1h", ["07:00"])  # 1h-кадр отстаёт по своему ТФ
        self._seed(cache, "SI", "1h", ["09:00"])

        assert cache.has_fresh_closed_bar("1m") is True
        assert cache.has_fresh_closed_bar("1h") is False

    def test_zero_tolerance_keeps_legacy_hard_and(self):
        clock = [datetime(2024, 1, 1, 10, 20)]
        cache = self._make(clock, 0)
        self._seed(cache, "SI", "1m", ["10:19"])
        self._seed(cache, "NG", "1m", ["10:16"])  # отстающая — блокирует при tol=0

        assert cache.has_fresh_closed_bar("1m") is False


class TestInitialLoadThrottle:
    """Разнос стартовых загрузок кадров (data_refresh_min_interval)."""

    def _make(self, clock_value, interval=5.0):
        clock = [clock_value]
        sched = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0])
        loader = FakeLoader({"NG": [pd.Timestamp("2024-01-01 10:10"),
                                    pd.Timestamp("2024-01-01 10:19")]})
        cache = MarketDataCache(loader=loader, timeline=sched, data_refresh_min_interval=interval)
        return cache

    def test_first_initial_load_not_throttled(self):
        cache = self._make(datetime(2024, 1, 1, 10, 20))
        inst = Instrument("NG", "NG", "future")
        with patch("src.data.cache.time.sleep") as sleep:
            cache.frame_for(inst, "1m")
        sleep.assert_not_called()

    def test_initial_load_waits_remaining_interval(self):
        cache = self._make(datetime(2024, 1, 1, 10, 20))
        cache._last_api_attempt = pd.Timestamp("2024-01-01 10:19:57")  # 3с назад, остаток 2с
        inst = Instrument("NG", "NG", "future")
        with patch("src.data.cache.time.sleep") as sleep:
            cache.frame_for(inst, "1m")
        sleep.assert_called_once_with(2.0)

    def test_initial_load_skips_wait_when_interval_elapsed(self):
        cache = self._make(datetime(2024, 1, 1, 10, 20))
        cache._last_api_attempt = pd.Timestamp("2024-01-01 10:19:54")  # прошло 6с >= 5с
        inst = Instrument("NG", "NG", "future")
        with patch("src.data.cache.time.sleep") as sleep:
            cache.frame_for(inst, "1m")
        sleep.assert_not_called()

    def test_interval_zero_disables_throttle(self):
        cache = self._make(datetime(2024, 1, 1, 10, 20), interval=0.0)
        cache._last_api_attempt = pd.Timestamp("2024-01-01 10:19:58")
        inst = Instrument("NG", "NG", "future")
        with patch("src.data.cache.time.sleep") as sleep:
            cache.frame_for(inst, "1m")
        sleep.assert_not_called()
