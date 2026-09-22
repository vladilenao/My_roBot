from datetime import datetime
from unittest.mock import patch

import pytest

from src.scheduler.timing import CandleScheduler, MultiTimeframeScheduler


def _utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss)


class TestCandleScheduler:
    def test_boundaries_are_naive(self):
        sched = CandleScheduler(timeframe="1h", clock=lambda: _utc(2024, 1, 1, 10, 37))
        assert sched.now().tzinfo is None
        assert sched.next_candle_close().tzinfo is None
        assert sched.current_candle_start().tzinfo is None
        assert sched.next_candle_close() == _utc(2024, 1, 1, 11, 0)

    def test_unsupported_timeframe_raises(self):
        with pytest.raises(ValueError, match="Неподдерживаемый таймфрейм"):
            CandleScheduler(timeframe="2h")

    def test_hour_boundary(self):
        sched = CandleScheduler(timeframe="1h", clock=lambda: _utc(2024, 1, 1, 10, 37))
        assert sched.next_candle_close() == _utc(2024, 1, 1, 11, 0)

    def test_five_minute_boundary(self):
        sched = CandleScheduler(timeframe="5m", clock=lambda: _utc(2024, 1, 1, 10, 7))
        assert sched.next_candle_close() == _utc(2024, 1, 1, 10, 10)

    def test_day_boundary(self):
        sched = CandleScheduler(timeframe="1d", clock=lambda: _utc(2024, 1, 1, 23, 59))
        assert sched.next_candle_close() == _utc(2024, 1, 2, 0, 0)

    def test_week_boundary_monday(self):
        # 2024-01-04 — четверг
        sched = CandleScheduler(timeframe="1w", clock=lambda: _utc(2024, 1, 4, 12, 0))
        assert sched.next_candle_close() == _utc(2024, 1, 8, 0, 0)

    def test_month_boundary(self):
        # январь -> 1 февраля
        sched = CandleScheduler(timeframe="1M", clock=lambda: _utc(2024, 1, 15, 12, 0))
        assert sched.next_candle_close() == _utc(2024, 2, 1, 0, 0)

    def test_fallback_secs(self):
        sched = CandleScheduler(timeframe="1h", sleep_secs=120.0)
        assert sched.fallback_secs() == 120.0

    def test_as_timestamp(self):
        sched = CandleScheduler(timeframe="1h", clock=lambda: _utc(2024, 1, 1, 10, 0))
        ts = sched.as_timestamp(_utc(2024, 1, 1, 10, 0))
        assert ts.tz is None

    def test_exact_boundary_has_zero_delay(self):
        # ровно в 10:00 для 1h текущая свеча 09:00-10:00 уже закрыта, следующая граница 11:00
        sched = CandleScheduler(timeframe="1h", clock=lambda: _utc(2024, 1, 1, 10, 0))
        assert sched.next_candle_close() == _utc(2024, 1, 1, 11, 0)

    def test_wait_until_candle_close_sleeps_and_returns_target(self):
        clock = _utc(2024, 1, 1, 10, 37)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)
        with patch("src.scheduler.timing.time.sleep") as mock_sleep:
            target = sched.wait_until_candle_close()
        mock_sleep.assert_called_once()
        assert target == _utc(2024, 1, 1, 11, 0)

    def test_wait_bar_published_immediate(self):
        clock = _utc(2024, 1, 1, 10, 0)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)
        calls = {"n": 0}

        def bar_ready():
            calls["n"] += 1
            return True

        with patch("src.scheduler.timing.time.monotonic", return_value=0.0), patch(
            "src.scheduler.timing.time.sleep"
        ):
            sched.wait_until_bar_published(bar_ready, poll_secs=1.0, timeout_secs=5.0)

        assert calls["n"] == 1

    def test_wait_bar_published_waits_until_ready(self):
        clock = _utc(2024, 1, 1, 10, 0)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)
        attempts = {"n": 0}

        def bar_ready():
            attempts["n"] += 1
            return attempts["n"] >= 3

        with patch("src.scheduler.timing.time.monotonic", return_value=0.0), patch(
            "src.scheduler.timing.time.sleep"
        ) as mock_sleep:
            sched.wait_until_bar_published(bar_ready, poll_secs=1.0, timeout_secs=5.0)

        assert attempts["n"] == 3
        mock_sleep.assert_called()

    def test_wait_bar_published_timeout(self):
        clock = _utc(2024, 1, 1, 10, 0)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)
        mono = {"t": 100.0}

        def fake_monotonic():
            mono["t"] += 10.0
            return mono["t"]

        calls = {"n": 0}

        def bar_ready():
            calls["n"] += 1
            return False

        with patch(
            "src.scheduler.timing.time.monotonic", side_effect=fake_monotonic
        ), patch("src.scheduler.timing.time.sleep"):
            sched.wait_until_bar_published(bar_ready, poll_secs=1.0, timeout_secs=5.0)

        assert calls["n"] >= 1

    def test_wait_bar_published_skips_boundary_when_wait_boundary_false(self):
        # середина периода: при wait_boundary=False сон до границы не выполняется
        clock = _utc(2024, 1, 1, 10, 37)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)

        def bar_ready():
            return True

        with patch("src.scheduler.timing.time.monotonic", return_value=0.0), patch(
            "src.scheduler.timing.time.sleep"
        ) as mock_sleep:
            sched.wait_until_bar_published(
                bar_ready, poll_secs=1.0, timeout_secs=5.0, wait_boundary=False
            )

        mock_sleep.assert_not_called()

    def test_wait_boundary_false_polls_with_timeout(self):
        clock = _utc(2024, 1, 1, 10, 37)
        sched = CandleScheduler(timeframe="1h", clock=lambda: clock)
        mono = {"t": 100.0}

        def fake_monotonic():
            mono["t"] += 1.0
            return mono["t"]

        calls = {"n": 0}

        def bar_ready():
            calls["n"] += 1
            return False

        with patch(
            "src.scheduler.timing.time.monotonic", side_effect=fake_monotonic
        ), patch("src.scheduler.timing.time.sleep") as mock_sleep:
            sched.wait_until_bar_published(
                bar_ready, poll_secs=1.0, timeout_secs=5.0, wait_boundary=False
            )

        assert calls["n"] >= 2
        mock_sleep.assert_called()

    def test_bar_close_hour(self):
        sched = CandleScheduler(timeframe="1h")
        assert sched.bar_close(_utc(2024, 1, 1, 8, 0)) == _utc(2024, 1, 1, 9, 0)

    def test_bar_close_week(self):
        # понедельник 2024-01-01 -> закрытие через 7 дней
        sched = CandleScheduler(timeframe="1w")
        assert sched.bar_close(_utc(2024, 1, 1)) == _utc(2024, 1, 8, 0, 0)

    def test_bar_close_month(self):
        sched = CandleScheduler(timeframe="1M")
        assert sched.bar_close(_utc(2024, 1, 1)) == _utc(2024, 2, 1, 0, 0)

    def test_bar_close_month_leap_year_february(self):
        # февраль високосного 2024 года закрывается 1 марта
        sched = CandleScheduler(timeframe="1M")
        assert sched.bar_close(_utc(2024, 2, 1)) == _utc(2024, 3, 1, 0, 0)

    def test_bar_close_month_december(self):
        sched = CandleScheduler(timeframe="1M")
        assert sched.bar_close(_utc(2024, 12, 1)) == _utc(2025, 1, 1, 0, 0)


class TestMultiTimeframeScheduler:
    def test_next_boundary_is_nearest_among_timeframes(self):
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: _utc(2024, 1, 1, 10, 37))
        assert s.next_boundary() == _utc(2024, 1, 1, 10, 45)

    def test_single_timeframe_matches_plain_scheduler(self):
        s = MultiTimeframeScheduler(["1h"], clock=lambda: _utc(2024, 1, 1, 10, 37))
        assert s.next_boundary() == _utc(2024, 1, 1, 11, 0)

    def test_non_divisible_mix_picks_nearest(self):
        s = MultiTimeframeScheduler(["15m", "1d"], clock=lambda: _utc(2024, 1, 1, 10, 37))
        assert s.next_boundary() == _utc(2024, 1, 1, 10, 45)

    def test_bootstrap_ready_all_timeframes(self):
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: _utc(2024, 1, 1, 10, 30))

        ready = s.wait_until_bar_published(lambda tf: True, wait_boundary=False)

        assert ready == {"1h", "15m"}

    def test_tick_off_hour_ready_only_15m(self):
        now = [_utc(2024, 1, 1, 10, 30)]
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: now[0])
        s.wait_until_bar_published(lambda tf: True, wait_boundary=False)

        # проснулись на границе 10:45 — пересеклась только сетка 15m
        now[0] = _utc(2024, 1, 1, 10, 45)
        with patch("src.scheduler.timing.time.sleep"):
            ready = s.wait_until_bar_published(lambda tf: True)

        assert ready == {"15m"}

    def test_tick_on_hour_ready_both(self):
        now = [_utc(2024, 1, 1, 10, 45)]
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: now[0])
        s.wait_until_bar_published(lambda tf: True, wait_boundary=False)

        now[0] = _utc(2024, 1, 1, 11, 0)
        with patch("src.scheduler.timing.time.sleep"):
            ready = s.wait_until_bar_published(lambda tf: True)

        assert ready == {"15m", "1h"}

    def test_publication_waited_per_tf(self):
        now = [_utc(2024, 1, 1, 10, 45)]
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: now[0])
        s.wait_until_bar_published(lambda tf: True, wait_boundary=False)

        now[0] = _utc(2024, 1, 1, 11, 0)
        attempts = {"1h": 0}

        def bar_ready(tf):
            if tf == "15m":
                return True
            attempts["1h"] += 1
            return attempts["1h"] >= 3

        with patch("src.scheduler.timing.time.sleep"):
            ready = s.wait_until_bar_published(bar_ready, poll_secs=1.0, timeout_secs=60.0)

        assert ready == {"15m", "1h"}
        assert attempts["1h"] == 3

    def test_publication_timeout_skips_unready_tf(self):
        now = [_utc(2024, 1, 1, 10, 45)]
        s = MultiTimeframeScheduler(["1h", "15m"], clock=lambda: now[0])
        s.wait_until_bar_published(lambda tf: True, wait_boundary=False)

        now[0] = _utc(2024, 1, 1, 11, 0)
        mono = {"t": 0.0}

        def fake_monotonic():
            mono["t"] += 100.0
            return mono["t"]

        with patch("src.scheduler.timing.time.sleep"), patch(
            "src.scheduler.timing.time.monotonic", side_effect=fake_monotonic
        ):
            ready = s.wait_until_bar_published(
                lambda tf: tf == "15m", poll_secs=1.0, timeout_secs=5.0
            )

        assert ready == {"15m"}

    def test_fallback_clamped_to_smallest_timeframe(self):
        s = MultiTimeframeScheduler(
            ["15m", "1h"], sleep_secs=3600, clock=lambda: _utc(2024, 1, 1, 10, 37)
        )
        assert s.fallback_secs() == 900.0

    def test_fallback_unchanged_when_below_smallest_tf(self):
        s = MultiTimeframeScheduler(
            ["1h"], sleep_secs=3600, clock=lambda: _utc(2024, 1, 1, 10, 37)
        )
        assert s.fallback_secs() == 3600.0

    def test_empty_timeframes_raise(self):
        with pytest.raises(ValueError):
            MultiTimeframeScheduler([])


class TestCatchUp:
    """Догон поздно опубликованных баров (catch_up_bars > 0)."""

    @staticmethod
    def _fast_timeout():
        state = {"t": 0.0}

        def monotonic():
            state["t"] += 100.0
            return state["t"]

        return monotonic

    def _tick(self, s, bar_ready, wait_boundary=True):
        with patch("src.scheduler.timing.time.sleep"), patch(
            "src.scheduler.timing.time.monotonic", self._fast_timeout()
        ):
            return s.wait_until_bar_published(
                bar_ready, poll_secs=1.0, timeout_secs=5.0,
                wait_boundary=wait_boundary,
            )

    def test_late_bar_within_horizon_is_ready_on_next_tick(self):
        now = [_utc(2024, 1, 1, 10, 15)]
        s = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: now[0], catch_up_bars=2)

        assert self._tick(s, lambda tf: False, wait_boundary=False) == set()

        # 10:16 — пересеклась только 1m, а просроченная 15m догоняется из pending
        now[0] = _utc(2024, 1, 1, 10, 16)
        ready = self._tick(s, lambda tf: tf == "15m")

        assert ready == {"15m"}

    def test_catch_up_holds_several_ticks_within_horizon(self):
        now = [_utc(2024, 1, 1, 10, 15)]
        s = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: now[0], catch_up_bars=2)
        self._tick(s, lambda tf: False, wait_boundary=False)

        now[0] = _utc(2024, 1, 1, 10, 16)
        assert self._tick(s, lambda tf: False) == set()

        now[0] = _utc(2024, 1, 1, 10, 17)
        ready = self._tick(s, lambda tf: tf == "15m")

        assert ready == {"15m"}

    def test_beyond_horizon_tf_is_dropped_and_ticks_continue(self):
        now = [_utc(2024, 1, 1, 10, 15)]
        s = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: now[0], catch_up_bars=2)
        self._tick(s, lambda tf: False, wait_boundary=False)

        # тик на границе 10:45 — 15m не готова, pending остаётся (старая граница 10:15)
        now[0] = _utc(2024, 1, 1, 10, 45)
        self._tick(s, lambda tf: False)

        # 10:46 — 15m не пересекалась с прошлого тика (последняя граница 10:45),
        # а её pending-запись старше горизонта 2×15m = 30 мин → отбрасывается
        now[0] = _utc(2024, 1, 1, 10, 46)
        with patch("src.scheduler.timing.log.warning") as mock_warn:
            ready = self._tick(s, lambda tf: True)

        assert ready == {"1m"}
        dropped = any(
            "15m" in str(call.args) and "потерян" in str(call.args)
            for call in mock_warn.call_args_list
        )
        assert dropped

    def test_catch_up_zero_keeps_legacy_behavior(self):
        now = [_utc(2024, 1, 1, 10, 15)]
        s = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: now[0], catch_up_bars=0)
        self._tick(s, lambda tf: False, wait_boundary=False)

        # без догона просроченная 15m не пере-опросется на 10:16
        now[0] = _utc(2024, 1, 1, 10, 16)
        ready = self._tick(s, lambda tf: True)

        assert ready == {"1m"}
        assert "15m" not in ready

    def test_bootstrap_pending_retried_without_boundary_wait(self):
        now = [_utc(2024, 1, 1, 10, 15)]
        s = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: now[0], catch_up_bars=2)

        ready = self._tick(s, lambda tf: tf == "1m", wait_boundary=False)

        assert ready == {"1m"}
        assert s._pending == {"15m": _utc(2024, 1, 1, 10, 15)}
