from datetime import datetime
from unittest.mock import patch

import pytest

from src.scheduler.timing import CandleScheduler


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
