from datetime import datetime, timedelta, timezone

import pytest

from src.scheduler.clock import HistoricalClock


def _clock(step_minutes=1, start="2024-01-01 00:00", end="2024-01-02 00:00", pause=1.0, sleeps=None):
    return HistoricalClock(
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        timedelta(minutes=step_minutes),
        pause,
        sleeper=sleeps if sleeps is not None else (lambda secs: None),
    )


class TestHistoricalClockStart:
    def test_starts_at_range_start(self):
        clock = _clock()

        assert clock.now() == datetime(2024, 1, 1, 0, 0)

    def test_registers_limits_and_step(self):
        clock = _clock(step_minutes=5)

        assert clock.start == datetime(2024, 1, 1, 0, 0)
        assert clock.end == datetime(2024, 1, 2, 0, 0)
        assert clock.step == timedelta(minutes=5)
        assert clock.is_virtual is True

    def test_first_tick_closes_first_bar(self):
        clock = _clock()

        clock.advance()

        assert clock.now() == datetime(2024, 1, 1, 0, 1)

    def test_ticks_total_counts_bars_in_range(self):
        assert _clock().ticks_total() == 24 * 60
        assert _clock(step_minutes=15).ticks_total() == 24 * 4
        assert _clock(start="2024-01-01 09:00", end="2024-01-01 10:00", step_minutes=5).ticks_total() == 12


class TestHistoricalClockSequence:
    def test_sequence_is_start_plus_k_step(self):
        clock = _clock(step_minutes=5, start="2024-01-01 09:00", end="2024-01-01 10:00")
        start, step = clock.start, clock.step
        moments = []
        while not clock.finished:
            clock.advance()
            moments.append(clock.now())

        expected = [start + step * k for k in range(1, 13)]
        assert moments == expected

    def test_bars_stay_inside_range(self):
        clock = _clock(step_minutes=5, start="2024-01-01 09:00", end="2024-01-01 10:00")
        bar_opens = []
        while not clock.finished:
            clock.advance()
            bar_opens.append(clock.now() - clock.step)

        assert bar_opens[0] == clock.start
        assert bar_opens[-1] == clock.end - clock.step
        assert all(clock.start <= bar < clock.end for bar in bar_opens)

    def test_last_tick_reaches_end(self):
        clock = _clock(step_minutes=15, start="2024-01-01 09:00", end="2024-01-01 10:00")
        ticks = 0
        while not clock.finished:
            clock.advance()
            ticks += 1

        assert clock.now() == clock.end
        assert ticks == clock.ticks_total()

    def test_advance_past_end_stops_at_end(self):
        clock = _clock(step_minutes=30, start="2024-01-01 09:00", end="2024-01-01 10:00")
        for _ in range(10):
            clock.advance()

        assert clock.now() == clock.end
        assert clock.finished is True

    def test_pause_does_not_change_moments(self):
        fast, slow = _clock(pause=0.0), _clock(pause=5.0)
        fast_moments, slow_moments = [], []
        while not fast.finished:
            fast.advance()
            fast_moments.append(fast.now())
        while not slow.finished:
            slow.advance()
            slow_moments.append(slow.now())

        assert fast_moments == slow_moments

    def test_pause_sleeps_between_ticks(self):
        slept = []
        clock = _clock(step_minutes=5, start="2024-01-01 09:00", end="2024-01-01 09:30", pause=1.5, sleeps=slept.append)
        clock.advance()

        assert slept == [1.5]
        assert clock.now() == datetime(2024, 1, 1, 9, 5)

    def test_zero_pause_does_not_sleep(self):
        slept = []
        clock = _clock(start="2024-01-01 09:00", end="2024-01-01 09:10", pause=0.0, sleeps=slept.append)
        clock.advance()

        assert slept == []


class TestHistoricalClockValidation:
    def test_rejects_inverted_range(self):
        with pytest.raises(ValueError):
            _clock(start="2024-01-02 00:00", end="2024-01-01 00:00")

    def test_rejects_empty_range(self):
        with pytest.raises(ValueError):
            _clock(start="2024-01-01 00:00", end="2024-01-01 00:00")

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            _clock(step_minutes=0)

    def test_accepts_aware_boundaries(self):
        clock = HistoricalClock(
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            timedelta(minutes=30),
            0.0,
        )

        assert clock.now() == datetime(2024, 1, 1, 0, 0)
        assert clock.finished is False
