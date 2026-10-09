from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from src.bot.run_session import HistoricalRunSession
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
    """Отдаёт заранее заданные бары по окну и считает попытки дозагрузки.

    Границы окна включительные с обеих сторон — как у настоящего источника: так
    тест ловит случаи, когда код полагается на исключающую семантику.

    ``error`` ломает любую загрузку, ``window_error`` — только запросы с явным
    окном (поиск следующего бара за разрывом).
    """

    def __init__(self, data, error=None, window_error=None):
        self.data = data
        self.error = error
        self.window_error = window_error
        self.calls = []
        self.windows = []
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
        self.windows.append((start_date, end_date))
        self.sources.append((client_provider, clock))
        if self.error is not None:
            raise self.error
        if end_date is not None and self.window_error is not None:
            raise self.window_error
        rows = [b for b in self.data if start_date is None or b >= pd.Timestamp(start_date)]
        if end_date is not None:
            rows = [b for b in rows if b <= pd.Timestamp(end_date)]
        return _rows(rows), "uid-1"


class HistoryHarness:
    """Кэш на виртуальных часах: шаг 1m, диапазон 00:00 … 01:00."""

    def __init__(self, bars, timeframes=("1m",), session=None, start=START, end=END, **kw):
        self.clock = HistoricalClock(
            start, end, timedelta(minutes=1), 0.0, sleeper=lambda secs: None
        )
        self.timeline = MultiTimeframeScheduler(list(timeframes), clock=self.clock)
        self.loader = RecordingLoader(bars)
        self.session = session if session is not None else HistoricalRunSession(self.clock)
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

    def run(self, count: int):
        """Тик целиком, как в бою: данные → закрытие тика → переход часов через разрыв."""
        for _ in range(count):
            if self.session.should_stop():
                break
            self.tick()
            if self.cache.data_exhausted:
                self.session.mark_data_exhausted(self.cache.data_exhaustion)
            gap = self.cache.take_pending_gap()
            if gap is not None:
                self.session.mark_data_gap(gap)
                self.timeline.reanchor()
        return self.cache

    def probe_windows(self):
        """Окна поиска следующего бара (единственные запросы с ``end_date``)."""
        return [window for window in self.loader.windows if window[1] is not None]


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

    def test_continuous_run_has_no_gaps_and_is_covered(self):
        """Без разрывов новый учёт молчит: счётчики пусты, диапазон пройден целиком."""
        harness = HistoryHarness(_minutes(60))

        cache = harness.run(60)

        assert cache.gaps == 0
        assert cache.missed_bars == 0
        assert cache.longest_gap == pd.Timedelta(0)
        assert cache.data_exhausted is False
        assert harness.session.covered is True

    def test_boundary_bars_give_360_missed_and_one_gap(self):
        """Крайние бары 00:00 и 06:01 на 1m: один разрыв и ровно 360 пропусков.

        Пустых тиков за разрыв много, но начисление одно — по границе разрыва.
        """
        bars = [pd.Timestamp("2024-01-01 00:00")] + _minutes(10, "2024-01-01 06:01")
        harness = HistoryHarness(
            bars, start=START, end=datetime(2024, 1, 1, 7, 0)
        )

        cache = harness.run(8)

        assert cache.gaps == 1
        assert cache.missed_bars == 360
        assert cache.longest_gap == pd.Timedelta(hours=6)


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
        assert harness.cache.missed_bars == 0

        cache = harness.ticks(1)

        assert cache._empty_ticks == 0
        assert cache.data_exhausted is False
        assert cache.missed_bars == 1

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


class TestHistoryGapJump:
    """Отсутствие баров в истории — разрыв в данных, а не конец прогона."""

    HOLE = [b for b in range(5, 10)]
    BARS = [
        b for b in _minutes(20)
        if b.minute not in (5, 6, 7, 8, 9) or b.hour != 0
    ]

    def _with_hole(self):
        bars = [
            pd.Timestamp("2024-01-01 00:00") + timedelta(minutes=i)
            for i in range(20)
            if i not in self.HOLE
        ]
        return bars

    def test_empty_ticks_report_next_available_bar_instead_of_data_end(self):
        harness = HistoryHarness(self._with_hole())

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is False
        gap = cache.take_pending_gap()
        assert gap.resume == pd.Timestamp("2024-01-01 00:10")
        assert gap.missed == 5
        assert gap.since == pd.Timestamp("2024-01-01 00:06")

    def test_pending_gap_handed_out_once(self):
        harness = HistoryHarness(self._with_hole())

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.take_pending_gap() is not None
        assert cache.take_pending_gap() is None

    def test_missed_bars_counted_once_for_whole_hole(self):
        """Разрыв в пять баров — это пять пропусков, а не по одному на тик."""
        harness = HistoryHarness(self._with_hole())

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.missed_bars == 5
        assert cache.gaps == 1

    def test_six_hour_pause_is_one_gap(self):
        """Пауза в шесть часов на 1m — один разрыв и 360 пропущенных баров."""
        bars = _minutes(5) + _minutes(10, "2024-01-01 06:05")
        harness = HistoryHarness(
            bars, start=START, end=datetime(2024, 1, 1, 7, 0)
        )

        cache = harness.run(8)

        assert cache.gaps == 1
        assert cache.missed_bars == 360
        assert cache.longest_gap == pd.Timedelta(hours=6)
        assert harness.session.market_now() == pd.Timestamp("2024-01-01 06:06")

    def test_gap_statistics_available_for_report(self):
        harness = HistoryHarness(self._with_hole())
        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.gaps == 1
        assert cache.longest_gap == pd.Timedelta(minutes=5)

    def test_frames_stay_closed_after_the_jump(self):
        """Переведённые часы не открывают бар на своём же моменте."""
        harness = HistoryHarness(
            [b for b in _minutes(60) if b.minute not in range(5, 10)]
        )

        harness.run(7)

        assert harness.session.market_now() == pd.Timestamp("2024-01-01 00:10")
        frame = harness.cache.frame_for(INSTRUMENT, "1m")
        assert pd.Timestamp("2024-01-01 00:10") not in list(frame["datetime"])
        assert frame["datetime"].max() == pd.Timestamp("2024-01-01 00:04")

    def test_empty_ticks_counter_starts_over_after_jump(self):
        harness = HistoryHarness(self._with_hole())

        harness.run(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert harness.cache._empty_ticks == 0

    def test_run_processes_bars_after_the_gap(self):
        harness = HistoryHarness(self._with_hole())

        harness.run(16)

        frame = harness.cache.frame_for(INSTRUMENT, "1m")
        assert pd.Timestamp("2024-01-01 00:14") in list(frame["datetime"])
        assert harness.cache.data_exhausted is False
        assert harness.session.market_now() == pd.Timestamp("2024-01-01 00:19")

    def test_jump_is_logged_with_both_moments(self, caplog):
        harness = HistoryHarness(self._with_hole())

        with caplog.at_level("INFO", logger="src.bot.run_session"):
            harness.run(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert any(
            "2024-01-01 00:06" in record.getMessage()
            and "2024-01-01 00:10" in record.getMessage()
            for record in caplog.records
        )

    def test_short_gap_closes_without_jumping(self):
        """Разрыв в один бар данные закрывают сами: рыночное время не двигается."""
        bars = [b for b in _minutes(20) if b != pd.Timestamp("2024-01-01 00:05")]
        harness = HistoryHarness(bars)

        harness.run(10)

        assert harness.cache.gaps == 1
        assert harness.cache.missed_bars == 1
        assert harness.session.market_now() == pd.Timestamp("2024-01-01 00:10")

    def test_probe_asks_only_the_driver_timeframe(self):
        harness = HistoryHarness(self._with_hole(), timeframes=("1m", "5m", "1h"))

        harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert harness.probe_windows()
        assert all(
            end is not None for _, end in harness.probe_windows()
        )

    def test_probe_window_stops_at_range_end(self):
        """Данные за концом диапазона не прыгают прогон вперёд."""
        bars = _minutes(5) + [
            pd.Timestamp("2024-01-01 00:00") + timedelta(minutes=90 + i)
            for i in range(5)
        ]
        harness = HistoryHarness(bars)

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is True
        assert cache.gaps == 0
        assert [end for _, end in harness.probe_windows()] == [pd.Timestamp(END)]

    def test_bar_at_current_moment_is_not_taken_as_next(self):
        """Источник отдаёт границы окна включительно: бар на текущем моменте не следующий.

        Настоящий источник включая обе границы окна, поэтому бар ровно на текущем
        рыночном моменте приходит в ответ на зонд и не должен выдавать «конец
        данных» — он ведь ещё не закрыт.
        """
        harness = HistoryHarness(_minutes(7))

        resume, failed = harness.cache._find_next_bar(pd.Timestamp("2024-01-01 00:05"))

        assert failed is False
        assert resume == pd.Timestamp("2024-01-01 00:06")

    def test_probe_window_stops_at_lookahead(self):
        clock = HistoricalClock(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-31"),
            timedelta(days=1), 0.0, sleeper=lambda secs: None,
        )
        timeline = MultiTimeframeScheduler(["1d"], clock=clock)
        loader = RecordingLoader(
            [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-20")]
        )
        cache = MarketDataCache(loader=loader, timeline=timeline, clock=clock)
        cache.frame_for(INSTRUMENT, "1d")

        for _ in range(3):
            clock.advance()
            cache.refresh_if_new_candle("1d")
            cache.close_tick()

        assert cache.data_exhausted is True
        assert [end for _, end in loader.windows if end is not None] == [
            pd.Timestamp("2024-01-11")
        ]

    def test_data_end_still_announced_without_next_bar(self):
        harness = HistoryHarness(_minutes(10))

        cache = harness.ticks(10 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is True
        assert cache.take_pending_gap() is None

    def test_probe_failure_does_not_pass_for_data_end(self):
        """Молчание источника не доказывает, что баров больше нет."""
        harness = HistoryHarness(self._with_hole())
        harness.ticks(6)
        harness.loader.window_error = RuntimeError("ресурс недоступен")

        harness.tick()

        cache = harness.cache
        assert cache.data_exhausted is False
        assert cache.gaps == 0
        assert cache.take_pending_gap() is None

    def test_probe_failure_does_not_lose_the_gap_start(self):
        harness = HistoryHarness(self._with_hole())
        harness.ticks(6)
        harness.loader.window_error = RuntimeError("ресурс недоступен")
        harness.tick()
        opened = harness.cache._gap
        assert opened is not None

        harness.tick()

        gap = harness.cache._gap
        assert gap is opened
        assert gap.since == opened.since
        assert gap.empty_ticks > CONSECUTIVE_EMPTY_TICKS_BEFORE_END

    def test_probe_recovers_after_source_failure(self):
        harness = HistoryHarness(self._with_hole())
        harness.ticks(6)
        harness.loader.window_error = RuntimeError("ресурс недоступен")
        harness.tick()

        harness.loader.window_error = None
        harness.tick()

        gap = harness.cache.take_pending_gap()
        assert gap is not None
        assert gap.resume == pd.Timestamp("2024-01-01 00:10")
        assert harness.cache.data_exhausted is False


class TestHistoryCheckedHorizon:
    """Конец данных всегда сообщает, какой участок поиска реально проверен."""

    def test_horizon_covers_whole_remainder_inside_lookahead(self):
        """Конец диапазона ближе семи суток: проверен остаток целиком."""
        bars = _minutes(5) + [
            pd.Timestamp("2024-01-01 00:00") + timedelta(minutes=90 + i)
            for i in range(5)
        ]
        harness = HistoryHarness(bars)

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)
        exhaustion = cache.data_exhaustion

        assert cache.data_exhausted is True
        assert exhaustion is not None
        assert exhaustion.horizon_start == exhaustion.at
        assert exhaustion.horizon_end == pd.Timestamp(END)
        assert exhaustion.range_end == pd.Timestamp(END)
        assert exhaustion.horizon_limited is False

    def test_horizon_is_limited_when_range_outlives_lookahead(self):
        """Диапазон длиннее семи суток: ограниченность поиска видна по границам."""
        clock = HistoricalClock(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-31"),
            timedelta(days=1), 0.0, sleeper=lambda secs: None,
        )
        timeline = MultiTimeframeScheduler(["1d"], clock=clock)
        loader = RecordingLoader(
            [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-20")]
        )
        cache = MarketDataCache(loader=loader, timeline=timeline, clock=clock)
        cache.frame_for(INSTRUMENT, "1d")

        for _ in range(3):
            clock.advance()
            cache.refresh_if_new_candle("1d")
            cache.close_tick()

        exhaustion = cache.data_exhaustion

        assert cache.data_exhausted is True
        assert exhaustion.horizon_end == pd.Timestamp("2024-01-11")
        assert exhaustion.range_end == pd.Timestamp("2024-01-31")
        assert exhaustion.horizon_limited is True

    def test_gap_longer_than_lookahead_stops_without_counting_gap(self):
        """Разрыв длиннее семи суток: прогон останавливается, разрыв не начисляется."""
        bars = [pd.Timestamp("2024-01-01 00:00")] + _minutes(5, "2024-01-09 00:00")
        harness = HistoryHarness(bars, start=START, end=datetime(2024, 1, 15))

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)
        exhaustion = cache.data_exhaustion

        assert cache.data_exhausted is True
        assert cache.gaps == 0
        assert cache.take_pending_gap() is None
        assert exhaustion.horizon_limited is True
        assert exhaustion.horizon_end == exhaustion.at + timedelta(days=7)
        assert exhaustion.range_end == pd.Timestamp("2024-01-15")

    def test_source_failure_keeps_exhaustion_unset(self):
        """Ошибка источника при поиске не объявляет ни конец данных, ни горизонт."""
        bars = _minutes(5) + _minutes(10, "2024-01-01 06:05")
        harness = HistoryHarness(bars, start=START, end=datetime(2024, 1, 1, 7, 0))
        harness.loader.window_error = RuntimeError("ресурс недоступен")

        cache = harness.ticks(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)

        assert cache.data_exhausted is False
        assert cache.data_exhaustion is None

    def test_exhaustion_reaches_session_with_horizon(self):
        """Сессия получает горизонт и строит причину завершения с его границами."""
        harness = HistoryHarness(_minutes(5), end=datetime(2024, 1, 1, 0, 30))

        harness.run(10)

        exhaustion = harness.session.exhaustion
        assert exhaustion is not None
        assert exhaustion.horizon_limited is False
        assert harness.session.covered is False
        reason = harness.session.stop_reason()
        assert "проверен весь остаток диапазона" in reason
        assert "2024-01-01 00:30" in reason
        assert "прогон не дошёл" in reason

    def test_limited_horizon_stops_run_with_honest_reason(self):
        """Остановленный поиском прогон не утверждает, что дальше данных нет."""
        bars = [pd.Timestamp("2024-01-01 00:00")] + _minutes(5, "2024-01-09 00:00")
        harness = HistoryHarness(bars, start=START, end=datetime(2024, 1, 15))

        harness.run(6 + CONSECUTIVE_EMPTY_TICKS_BEFORE_END)
        reason = harness.session.stop_reason()

        assert harness.session.should_stop() is True
        assert "следующий бар не найден в проверенном горизонте" in reason
        assert "после него данные не проверялись" in reason
        assert "не дойдено" in reason


class TestHistoryRunToEnd:
    """Прогон целиком: разрыв перепрыгивается, конец данных честно останавливает."""

    def test_range_covered_after_jumping_over_gap(self):
        harness = HistoryHarness(
            [b for b in _minutes(60) if b.minute not in range(5, 10)]
        )

        harness.run(60)

        assert harness.session.covered is True
        assert harness.session.should_stop() is True
        assert "обработан полностью" in harness.session.stop_reason()
        assert harness.cache.gaps == 1

    def test_data_ending_mid_range_stops_run_without_coverage(self):
        harness = HistoryHarness(_minutes(10))

        harness.run(15)

        session = harness.session
        assert session.should_stop() is True
        assert session.covered is False
        assert "данные закончились" in session.stop_reason()
        assert "2024-01-01 01:00" in session.stop_reason()

    def test_exhaustion_after_the_last_bar_is_reported_once(self):
        harness = HistoryHarness(_minutes(10))
        harness.run(15)
        exhausted_at = harness.session.market_now()

        harness.run(3)

        assert harness.session.market_now() == exhausted_at
        assert harness.session.stop_reason().count("данные закончились") == 1


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

    def test_live_never_searches_next_bar(self):
        """Боевой режим не ищет следующий бар: пауза в данных не двигает рынок."""
        cache, loader = self._live([])
        cache.frame_for(INSTRUMENT, "1h")

        cache.close_tick()
        cache.close_tick()

        assert all(end is None for _, end in loader.windows)
        assert cache.data_exhausted is False
        assert cache.take_pending_gap() is None
        assert cache.gaps == 0


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
