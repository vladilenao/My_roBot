from datetime import datetime, timedelta, timezone
from time import perf_counter

import pytest

from src.market_structure.worker import PatternTask, PatternWorker


class Store:
    def __init__(self):
        self.events = []
        self.delivered = []

    def pattern_context(self, **_):
        return {"has_gap": False, "missing_open_time": None, "candles": [
            {"open": 10, "high": 11, "low": 8, "close": 9},
            {"open": 8, "high": 12, "low": 7, "close": 11},
        ]}

    def enqueue_pattern_event(self, **event):
        self.events.append(event)

    def pending_pattern_events(self, **_):
        return list(self.events)

    def mark_pattern_event_delivered(self, event_id):
        self.delivered.append(event_id)
        self.events = [event for event in self.events if event["event_id"] != event_id]

    def defer_pattern_event(self, *_):
        self.deferred = True


class Redis:
    def __init__(self):
        self.messages = []

    def time(self):
        return 1_767_225_600, 0

    def xadd(self, stream, fields):
        self.messages.append((stream, fields))

    def xtrim(self, *_args, **_kwargs):
        return 0


class BrokenRedis(Redis):
    def time(self):
        raise ConnectionError("temporarily unavailable")


class GapStore(Store):
    def pattern_context(self, **_):
        return {"has_gap": True, "missing_open_time": "2026-01-01T00:00:00Z", "candles": []}


def test_submit_is_nonblocking_and_deduplicates_same_closed_bar():
    worker = PatternWorker(Store(), None)

    assert worker.submit(ticker="SBER", instrument_uid="uid", timeframe="1m",
                         close_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert not worker.submit(ticker="SBER", instrument_uid="uid", timeframe="1m",
                             close_time=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_submit_drops_new_task_when_queue_is_full(caplog):
    worker = PatternWorker(Store(), None, queue_maxsize=1)

    assert worker.submit(ticker="SBER", instrument_uid="uid", timeframe="1m",
                         close_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert not worker.submit(ticker="SBER", instrument_uid="uid", timeframe="1m",
                             close_time=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))

    assert "Очередь market_structure заполнена: SBER 1m" in caplog.text


def test_submit_p99_for_ten_thousand_closed_bars_is_under_five_milliseconds():
    worker = PatternWorker(Store(), None, queue_maxsize=10_000)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    durations = []

    for offset in range(10_000):
        begin = perf_counter()
        assert worker.submit(ticker="SBER", instrument_uid="uid", timeframe="1m",
                             close_time=started + timedelta(microseconds=offset))
        durations.append(perf_counter() - begin)

    p99 = sorted(durations)[9_899]
    assert p99 <= 0.005, f"p99 постановки в очередь: {p99 * 1_000:.3f} мс"


def test_analysis_writes_deterministic_event_to_outbox():
    store = Store()
    worker = PatternWorker(store, None)
    worker._analyze(PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z"))

    assert len(store.events) == 1
    assert store.events[0]["event_id"]


def test_publish_adds_published_at_without_storing_it_in_outbox():
    store, redis = Store(), Redis()
    worker = PatternWorker(store, redis)

    worker._analyze(PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z"))

    assert store.delivered
    assert redis.messages[0][0] == "market.events"
    assert "published_at" in redis.messages[0][1]


def test_redis_failure_defers_outbox_event_without_raising():
    store = Store()
    worker = PatternWorker(store, BrokenRedis())

    worker._analyze(PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z"))

    assert getattr(store, "deferred", False)


def test_gap_requests_live_recovery_and_keeps_task_unfinished():
    requested = []
    worker = PatternWorker(GapStore(), None, recovery_requester=lambda *args: requested.append(args))

    complete = worker._analyze(PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z"))

    assert not complete
    assert requested == [("uid", "1m", "2026-01-01T00:00:00Z")]


def test_completed_recovery_reanalyses_context_and_writes_event():
    class RecoveringStore(Store):
        def __init__(self):
            super().__init__()
            self.recovered = False

        def pattern_context(self, **_):
            if not self.recovered:
                return {"has_gap": True, "missing_open_time": "2026-01-01T00:00:00Z", "candles": []}
            return super().pattern_context()

    store = RecoveringStore()
    worker = PatternWorker(store, None, recovery_requester=lambda *_: setattr(store, "recovered", True))
    task = PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z")

    assert not worker._analyze(task)
    assert worker._analyze(task)
    assert len(store.events) == 1


def test_no_pattern_writes_debug_log_without_event(caplog):
    class NoPatternStore(Store):
        def pattern_context(self, **_):
            return {"has_gap": False, "missing_open_time": None, "candles": [
                {"open": 10, "high": 11, "low": 8, "close": 9},
                {"open": 9, "high": 10, "low": 7, "close": 8},
            ]}

    store = NoPatternStore()
    worker = PatternWorker(store, None)

    with caplog.at_level("DEBUG"):
        assert worker._analyze(PatternTask("SBER", "uid", "1m", "2026-01-01T00:01:00Z"))

    assert store.events == []
    assert "паттернов не обнаружено" in caplog.text
