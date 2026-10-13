"""Приёмка reliable-live-candle-ingestion (design: MVP Acceptance Matrix).

Сценарии матрицы проверяются независимыми тестами:

1. normal stream, ACK/handoff — test_stream_ingestion::test_each_pair_needs_ack_then_overlap_handoff_and_partial_ack_falls_back,
   ::test_candle_before_ack_is_not_accepted_and_after_ack_is_closed_input; фильтровый ТФ — ниже.
2. late candle и deadline — test_live_causal_processing::test_new_high_water_does_not_clear_deadline_of_internal_gap,
   ::test_effective_availability_is_not_before_input_publication, test_live_analysis (поздний бар — история).
3. unary-only recovery — test_ingestion_worker::test_trailing_gap_waits_for_publication_deadline_then_recovers.
4. disconnect 5 минут — test_ingestion_worker::test_full_outage_is_recovered_without_one_hour_cutoff[outage0],
   test_stream_ingestion::test_reconnect_requires_new_ack_and_old_epoch_does_not_close_candles,
   test_live_causal_processing::test_out_of_order_minutes_wait_for_missing_and_run_in_time_order_once.
5. disconnect 3 часа — test_ingestion_worker::test_full_outage_is_recovered_without_one_hour_cutoff[outage1].
6. duplicate/out-of-order — test_live_causal_processing::test_duplicate_stream_and_rpc_minute_is_executed_once.
7. correction after decision/commit — test_live_execution_pin::test_correction_during_started_package_becomes_integrity_hold,
   test_live_causal_processing::test_integrity_hold_of_executed_minute_survives_restart_and_reconnect.
8. no trades/quiet stream — test_stream_ingestion::test_quiet_session_with_ping_is_healthy,
   test_ingestion_worker::test_empty_responses_park_range_without_fast_cycle_and_survive_restart,
   ::test_calendar_break_needs_no_candle_but_unknown_schedule_is_recovered.
9. intraday restart — test_live_execution_pin::test_restart_after_financial_commit_does_not_replay_minute,
   test_ingestion_worker::test_saved_obligations_register_management_timeframe_and_execution_lower.
10. несколько UID/ТФ, один критический gap — ниже.

Fault cases (задача 8.2): переполнение буфера, забытая подписка, invalid source/interval,
auth/quota, stale generation, blocked read-stop, correction during commit, restart after
financial commit — в test_stream_ingestion и test_live_execution_pin; сквозной старт — ниже.
"""

from datetime import datetime, timezone
from decimal import Decimal
from queue import Empty, Queue
from unittest.mock import MagicMock, patch
import threading

import pandas as pd

import run
from src.api.market_data_stream import CandleSubscriptionAck, StreamCandle, StreamResponse
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.market_store import MarketDataStore
from src.data.poller import LiveMarketDataPoller
from src.data.request_budget import RequestBudget
from src.data.stream_ingestion import StreamSupervisor
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


def bars(start, count, price=100):
    stamps = pd.date_range(pd.Timestamp(start), periods=count, freq="1min")
    return pd.DataFrame({"datetime": stamps, "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1, "is_complete": True})


def test_critical_gap_of_one_uid_does_not_stop_healthy_uid(tmp_path):
    now = datetime(2026, 10, 12, 12, 3, 30)
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: now)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), kw["instrument_id"]), timeline,
                            request_budget=RequestBudget(lambda: now, 0), market_store=store)
    a, b = (Instrument(name, name, "share", name, name.lower()) for name in ("AAA", "BBB"))
    for item in (a, b):
        # Сохранённые обязательства передаются до приёма данных, как в run.py:
        # граница исполнения строгая и не заменяется cutover запуска.
        cache.note_execution_lower(item, pd.Timestamp("2026-10-12 12:00"))
        cache.register_live_pair(item, item.uid, "1m")
        cache.mark_live_history(item.uid, "1m", done=True)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:00", 1).iloc[[0]], received_at=now)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:02", 1), received_at=now)
    store.set_interval_state(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                             open_time="2026-10-12 12:01", state="pending", evidence="open_session_missing_candle")
    cache.accept_rpc_frame("bbb", "1m", bars("2026-10-12 12:00", 3), received_at=now)
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    storage = MagicMock()
    storage.load_trades.return_value = ()
    start = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    storage.execution_bar_boundaries.side_effect = lambda: {"aaa": (start, True), "bbb": (start, True)}
    with patch("run.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("run._load_contracts_metadata", return_value={}), patch("run.print_contract_metadata"), patch("run._risk_limits"):
        runtime = run._build_runtime([a, b], cache, MagicMock(), tmp_path)
    assert runtime.post_tick({"1m"}) is True
    executed = {}
    for call in broker.track_bar.call_args_list:
        for uid in call.args[1]:
            executed.setdefault(uid, []).append(call.args[0].minute)
    assert executed["bbb"] == [0, 1, 2]  # здоровый UID продолжает
    assert executed["aaa"] == [0]  # A не перескакивает неизвестную 12:01
    assert cache.execution_ready(b, pd.Timestamp("2026-10-12 12:02"))
    assert not cache.execution_ready(a, pd.Timestamp("2026-10-12 12:02"))


class Session:
    def __init__(self):
        self.responses = Queue()
        self.closed = threading.Event()
        self.sent = []

    def open(self):
        return self

    def configure_ping(self, interval_ms):
        pass

    def subscribe(self, pairs):
        self.sent.append(tuple(pairs))

    def subscriptions(self):
        pass

    def __iter__(self):
        while not self.closed.is_set():
            try:
                yield self.responses.get(timeout=0.02)
            except Empty:
                continue

    def close(self):
        self.closed.set()


def test_end_to_end_stream_startup_overlap_internal_and_trailing_gap(tmp_path):
    clock = [datetime(2026, 10, 12, 12, 5, 10)]
    timeline = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: clock[0])
    store = MarketDataStore(tmp_path / "market.sqlite3")
    # Источник истории знает 11:30–12:04, кроме внутренней 11:50 (появится позже).
    exchange = bars("2026-10-12 11:30", 35)
    exchange = exchange[exchange["datetime"] != pd.Timestamp("2026-10-12 11:50")]
    source = {"frame": exchange}
    def loader(**kwargs):
        frame = source["frame"]
        keep = pd.Series(True, index=frame.index)
        if kwargs["start_date"] is not None:
            keep &= frame["datetime"] >= pd.Timestamp(kwargs["start_date"])
        if kwargs["end_date"] is not None:
            keep &= frame["datetime"] < pd.Timestamp(kwargs["end_date"])
        return frame[keep].reset_index(drop=True), "u"
    # Робот работает с 11:30: внутренняя дыра — пропуск во время работы.
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0), market_store=store,
                            live_cutover=datetime(2026, 10, 12, 11, 30))
    item = Instrument("SBER", "SBER", "share", "SBER", "u")
    cache.prepare_live_history(item, "1m", 30)
    settings = MarketDataSettings(live_transport="stream", candle_publication_timeout_secs=30,
                                  stream_ack_timeout_seconds=5)
    worker = IngestionWorker(cache, settings=settings)
    session = Session()
    stream = StreamSupervisor(cache, worker, lambda: session, settings=settings, wall=lambda: clock[0])
    poller = LiveMarketDataPoller(cache, ["1m"], 0.05, worker=worker, stream=stream)
    poller.start()
    threads = list(poller._threads)
    try:
        assert poller.wait_startup(timeout=0) is False  # нет ACK — нет готовности
        for _ in range(200):
            if session.sent:
                break
            threading.Event().wait(0.01)
        session.responses.put(StreamResponse("subscribe_candles_response", acknowledgements=(
            CandleSubscriptionAck("u", "1m", 1, 1, True, None, "s", "sub", "t"),)))
        assert poller.wait_startup(timeout=5)
        assert cache.pair_status(item, "1m")["state"] in {"READY", "GAP_HOLD"}
        # Внутренняя 11:50 теперь доступна в истории, свеча 12:05 придёт потоком.
        source["frame"] = bars("2026-10-12 11:30", 36)
        clock[0] = datetime(2026, 10, 12, 12, 6, 50)
        session.responses.put(StreamResponse("candle", candle=StreamCandle("u", "1m", datetime(2026, 10, 12, 12, 5),
            datetime(2026, 10, 12, 12, 5, 40), (Decimal(100), Decimal(101), Decimal(99), Decimal(100)), 1, 1)))
        for _ in range(500):
            stamps = set(cache.frame_for(item, "1m")["datetime"])
            if pd.Timestamp("2026-10-12 11:50") in stamps and pd.Timestamp("2026-10-12 12:05") in stamps:
                break
            poller.wake()
            threading.Event().wait(0.01)
        frame = cache.frame_for(item, "1m")
        assert list(frame["datetime"]) == list(pd.date_range("2026-10-12 11:30", "2026-10-12 12:05", freq="1min"))
        assert frame["datetime"].is_unique  # стык истории и потока без дубля
    finally:
        poller.close()
    assert len(threads) == 2 and not any(thread.is_alive() for thread in threads)  # stop прервал чтение
