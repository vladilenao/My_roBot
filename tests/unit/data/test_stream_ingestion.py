"""Stream-полоса: подтверждённые подписки, здоровье, reconnect, буфер, fallback."""

from datetime import datetime
from decimal import Decimal
from queue import Empty, Queue
from types import SimpleNamespace
import threading

import pandas as pd

from src.api.market_data_stream import CandleSubscriptionAck, StreamCandle, StreamResponse
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker, RecoveryRange
from src.data.ingestion_settings import MarketDataSettings
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.data.stream_ingestion import StreamSupervisor
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

UID = "u"
ITEM = Instrument("SBER", "SBER", "share", "SBER", UID)
NOW = datetime(2026, 10, 12, 12, 2, 30)


class FakeSession:
    def __init__(self):
        self.sent = []
        self.responses = Queue()
        self.closed = threading.Event()
        self.pings = []

    def open(self):
        return self

    def configure_ping(self, interval_ms):
        self.pings.append(interval_ms)

    def subscribe(self, pairs):
        self.sent.append(("subscribe", tuple(pairs)))

    def subscriptions(self):
        self.sent.append(("check",))

    def __iter__(self):
        while not self.closed.is_set():
            try:
                item = self.responses.get(timeout=0.02)
            except Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        self.closed.set()


class RpcError(Exception):
    def __init__(self, name):
        super().__init__(name)
        self.name = name

    def code(self):
        return SimpleNamespace(name=self.name)


def ack(tf, status=1, *, source=None, waiting_close=True):
    return StreamResponse("subscribe_candles_response", acknowledgements=(
        CandleSubscriptionAck(UID, tf, status, 1, waiting_close, source, "stream", "sub", "track"),))


def candle(minute, price=100):
    return StreamResponse("candle", candle=StreamCandle(UID, "1m", datetime(2026, 10, 12, 12, minute),
        datetime(2026, 10, 12, 12, minute, 30), tuple(Decimal(value) for value in (price, price + 1, price - 1, price)), 1, 1))


def setup(tmp_path, *, settings=None, clock=None):
    timeline = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: NOW)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    history = pd.DataFrame({"datetime": [pd.Timestamp("2026-10-12 12:00")], "open": 100, "high": 101,
                            "low": 99, "close": 100, "volume": 1, "is_complete": True})
    calls = []
    def loader(**kwargs):
        calls.append(kwargs["timeframe"])
        return history, UID
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: NOW, 0), market_store=store)
    for tf in ("1m", "15m"):
        cache.register_live_pair(ITEM, UID, tf)
        cache.mark_live_history(UID, tf, done=True)
    settings = settings or MarketDataSettings(stream_ack_timeout_seconds=5, stream_health_timeout_seconds=20,
                                              stream_ping_interval_ms=15000)
    worker = IngestionWorker(cache, settings=settings)
    sessions = []
    def factory():
        sessions.append(FakeSession())
        return sessions[-1]
    sleeps = []
    supervisor = StreamSupervisor(cache, worker, factory, settings=settings, clock=clock or (lambda: 0.0),
                                  wall=lambda: NOW, sleep=sleeps.append, jitter=lambda: 0.5)
    return cache, store, worker, supervisor, sessions, sleeps, calls


def wait(predicate, timeout=2):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        deadline.wait(0.01)
    return predicate()


def start(supervisor):
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    return thread


def test_each_pair_needs_ack_then_overlap_handoff_and_partial_ack_falls_back(tmp_path):
    cache, store, worker, supervisor, sessions, _, calls = setup(tmp_path)
    thread = start(supervisor)
    assert wait(lambda: sessions and sessions[0].sent)
    assert sessions[0].pings == [15000]
    assert set(sessions[0].sent[0][1]) == {(UID, "1m"), (UID, "15m")}
    sessions[0].responses.put(ack("1m"))
    sessions[0].responses.put(ack("15m", status=3))  # invalid interval
    assert wait(lambda: supervisor.pairs[UID, "1m"]["status"] == "acknowledged")
    assert wait(lambda: supervisor.pairs[UID, "15m"]["status"] == "failed")
    assert supervisor.pairs[UID, "15m"]["reason"] == "invalid_interval"
    assert cache.pair_status(ITEM, "1m")["state"] == "RECONCILING"  # ACK ещё не READY
    worker.run_until_idle()  # исторический overlap до фиксированного H
    assert supervisor.pairs[UID, "1m"]["status"] == "ready"
    assert cache.pair_status(ITEM, "1m")["state"] == "READY"
    assert cache.pair_status(ITEM, "15m")["transport"] == "polling" and supervisor.fallback
    assert worker._stream_pairs == {(UID, "1m")}
    supervisor.close()
    thread.join(2)
    assert not thread.is_alive()  # cancel прерывает блокирующее чтение


def test_candle_before_ack_is_not_accepted_and_after_ack_is_closed_input(tmp_path):
    cache, store, worker, supervisor, sessions, _, _ = setup(tmp_path)
    thread = start(supervisor)
    assert wait(lambda: sessions and sessions[0].sent)
    sessions[0].responses.put(candle(1, 101))
    sessions[0].responses.put(ack("1m"))
    assert wait(lambda: supervisor.pairs[UID, "1m"]["status"] == "acknowledged")
    assert cache.frame_for(ITEM, "1m").empty  # событие до ACK не стало входом
    sessions[0].responses.put(candle(1, 101))
    assert wait(lambda: len(cache.frame_for(ITEM, "1m")) == 1)
    assert cache.frame_for(ITEM, "1m")["close"].iloc[-1] == 101
    supervisor.close()
    thread.join(2)


def test_invalid_source_or_unconfirmed_waiting_close_is_rejected(tmp_path):
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path)
    supervisor.set_roster({(UID, "1m"), (UID, "15m")})
    supervisor._receive(ack("1m", source=1), epoch=0)
    supervisor._receive(ack("15m", waiting_close=False), epoch=0)
    assert supervisor.pairs[UID, "1m"]["reason"] == "invalid_source"
    assert supervisor.pairs[UID, "15m"]["reason"] == "waiting_close_not_confirmed"
    assert cache._live_evidence == {}


def test_health_timeout_and_ack_timeout(tmp_path):
    now = [0.0]
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path, clock=lambda: now[0])
    session = FakeSession()
    supervisor.set_roster({(UID, "1m"), (UID, "15m")})
    supervisor.epoch = 1
    supervisor._subscribe(session, 1)
    supervisor._last_response = 0.0
    supervisor._receive(ack("1m"), epoch=1)
    now[0] = 6.0
    assert supervisor._watch(session, 1)
    assert supervisor.pairs[UID, "15m"]["reason"] == "ack_timeout"
    now[0] = 21.0  # ни ping, ни ответа дольше health timeout
    assert not supervisor._watch(session, 1)
    assert session.closed.is_set() and supervisor.last_error == "health_timeout"


def test_quiet_session_with_ping_is_healthy(tmp_path):
    now = [0.0]
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path, clock=lambda: now[0])
    session = FakeSession()
    supervisor._last_response = 0.0
    for second in range(5, 60, 5):
        now[0] = float(second)
        supervisor._receive(StreamResponse("ping", server_time=NOW), epoch=0)
        assert supervisor._watch(session, 0)
    assert not session.closed.is_set()


def test_forgotten_subscription_is_resubscribed_and_gap_recovered(tmp_path):
    now = [0.0]
    cache, store, worker, supervisor, _, _, _ = setup(tmp_path, clock=lambda: now[0])
    session = FakeSession()
    supervisor.set_roster({(UID, "1m")})
    supervisor.epoch = 1
    supervisor._subscribe(session, 1)
    supervisor._receive(ack("1m"), epoch=1)
    supervisor._handoff_ready(UID, "1m", 1)
    supervisor._last_open[UID, "1m"] = datetime(2026, 10, 12, 12, 0)
    supervisor.connection = "ACTIVE"
    now[0] = 400.0
    supervisor._last_response = now[0]
    supervisor._watch(session, 1)
    assert ("check",) in session.sent
    checked = len(session.sent)
    now[0] = 406.0
    supervisor._last_response = now[0]
    supervisor._watch(session, 1)  # ответ на проверку не содержит пару
    assert any(kind == "subscribe" and (UID, "1m") in pairs for kind, *rest in session.sent[checked:] for pairs in rest)
    assert RecoveryRange.from_json(store.live_state(kind="recovery", source="tbank_exchange",
                                                    instrument_uid=UID, interval="1m")) is not None


def test_reconnect_backoff_is_bounded_with_jitter_and_permanent_errors_are_slow(tmp_path):
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path, settings=MarketDataSettings(
        stream_reconnect_initial_secs=1, stream_reconnect_max_secs=30, stream_reconnect_jitter_pct=20,
        stream_control_cooldown_secs=60))
    delays = []
    for failures in range(1, 8):
        supervisor._failures = failures
        delays.append(supervisor._delay(RuntimeError("UNAVAILABLE"), False))
    assert delays[:5] == [1, 2, 4, 8, 16] and max(delays) == 30
    supervisor._jitter = lambda: 1.0
    supervisor._failures = 1
    assert supervisor._delay(RuntimeError("x"), False) == 1.2
    assert supervisor._delay(RpcError("UNAUTHENTICATED"), True) == 60
    quota = RpcError("RESOURCE_EXHAUSTED")
    quota.trailing_metadata = lambda: (("ratelimit-reset", "17"),)
    assert supervisor._delay(quota, False) >= 1


def test_permanent_error_is_not_retried_fast(tmp_path):
    cache, _, worker, supervisor, sessions, sleeps, _ = setup(tmp_path)
    def failing():
        if len(sleeps) >= 2:
            supervisor._stopped.set()
        raise RpcError("UNAUTHENTICATED")
    supervisor._factory = failing
    supervisor.run()
    assert sleeps and all(delay >= 60 for delay in sleeps)
    assert supervisor.last_error.startswith("UNAUTHENTICATED")


def test_buffer_overflow_starts_range_recovery_immediately(tmp_path):
    settings = MarketDataSettings(stream_buffer_max_events=1)
    cache, store, worker, supervisor, _, _, _ = setup(tmp_path, settings=settings)
    supervisor.set_roster({(UID, "1m")})
    supervisor.epoch = 1
    supervisor.pairs[UID, "1m"].update(status="ready", epoch=1)
    supervisor._receive(candle(1), epoch=1)
    supervisor._receive(candle(2), epoch=1)
    assert supervisor.overflows == 1
    book = RecoveryRange.from_json(store.live_state(kind="recovery", source="tbank_exchange",
                                                    instrument_uid=UID, interval="1m"))
    assert book is not None and book.reason == "buffer_overflow" and book.next_retry_at <= pd.Timestamp(NOW)
    assert cache.pair_status(ITEM, "1m")["state"] == "GAP_HOLD"


def test_fallback_disabled_holds_pairs_instead_of_polling(tmp_path):
    settings = MarketDataSettings(stream_fallback_polling=False)
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path, settings=settings)
    supervisor.set_roster({(UID, "1m"), (UID, "15m")})
    supervisor._fail((UID, "1m"), "ack_timeout")
    assert (UID, "1m") in worker._held_pairs
    assert not worker._polled({"uid": UID, "timeframe": "1m"})
    assert cache.pair_status(ITEM, "1m")["state"] == "RECONCILING"
    assert not supervisor.fallback


def test_reconnect_requires_new_ack_and_old_epoch_does_not_close_candles(tmp_path):
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path)
    supervisor.set_roster({(UID, "1m")})
    supervisor.epoch = 1
    supervisor._receive(ack("1m"), epoch=1)
    supervisor._handoff_ready(UID, "1m", 1)
    supervisor._disconnected("disconnect")
    assert supervisor.pairs[UID, "1m"]["status"] == "desired"
    assert cache._live_evidence == {}
    assert cache.pair_status(ITEM, "1m")["state"] == "RECONCILING"
    stale = cache.accept_stream_candle(candle(1).candle, epoch=1, received_at=NOW)
    assert stale["state"] == "preliminary"
    # Готовность старого поколения не открывает новое.
    supervisor.epoch = 2
    supervisor._receive(ack("1m"), epoch=2)
    supervisor._handoff_ready(UID, "1m", 1)
    assert supervisor.pairs[UID, "1m"]["status"] == "acknowledged"


def test_restart_with_cached_frame_is_not_ready_without_ack_and_handoff(tmp_path):
    cache, store, worker, supervisor, _, _, _ = setup(tmp_path)
    cache.accept_rpc_frame(UID, "1m", pd.DataFrame({"datetime": [pd.Timestamp("2026-10-12 12:00")], "open": 100,
        "high": 101, "low": 99, "close": 100, "volume": 1, "is_complete": True}), received_at=NOW)
    cache.set_live_handoff(UID, "1m", transport="stream", epoch=None, ready=False)
    assert not cache.frame_for(ITEM, "1m").empty
    assert cache.pair_status(ITEM, "1m")["state"] == "RECONCILING"


def test_over_capacity_pairs_fall_back_without_resetting_cursors(tmp_path):
    settings = MarketDataSettings(stream_subscription_soft_limit=1)
    cache, store, worker, supervisor, _, _, _ = setup(tmp_path, settings=settings)
    store.mark_processed(source="tbank_exchange", instrument_uid=UID, interval="1m", open_time="2026-10-12 12:00")
    session = FakeSession()
    supervisor.set_roster({(UID, "1m"), (UID, "15m")})
    supervisor.epoch = 1
    supervisor._subscribe(session, 1)
    failed = [pair for pair, info in supervisor.pairs.items() if info["status"] == "failed"]
    assert len(failed) == 1 and supervisor.pairs[failed[0]]["reason"] == "over_capacity"
    assert supervisor.fallback and worker._polled({"uid": failed[0][0], "timeframe": failed[0][1]})
    assert store.cursors(source="tbank_exchange", instrument_uid=UID, interval="1m")["processed_through"] is not None


def test_too_many_requests_pauses_then_resubscribes_and_capacity_prefers_base_1m(tmp_path):
    now = [0.0]
    settings = MarketDataSettings(stream_control_cooldown_secs=60, stream_subscription_soft_limit=1)
    cache, _, worker, supervisor, _, _, _ = setup(tmp_path, settings=settings, clock=lambda: now[0])
    session = FakeSession()
    supervisor.set_roster({(UID, "1m"), (UID, "15m")})
    supervisor.epoch = 1
    supervisor._subscribe(session, 1)
    assert session.sent[0] == ("subscribe", ((UID, "1m"),))  # 1m не вытеснен по ёмкости
    assert supervisor.pairs[UID, "15m"]["reason"] == "over_capacity"
    supervisor._receive(ack("1m", status=6), epoch=1)
    assert supervisor.pairs[UID, "1m"]["reason"] == "too_many_requests"
    supervisor._subscribe(session, 1)
    assert len(session.sent) == 1  # немедленной рассылки нет
    now[0] = 61.0
    supervisor._subscribe(session, 1)
    assert session.sent[-1] == ("subscribe", ((UID, "1m"),))
