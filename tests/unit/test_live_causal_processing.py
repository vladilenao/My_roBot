"""Причинная обработка live-входа: допуск доставки, exactly-once и коррекции."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import sqlite3

import pandas as pd

import run
from src.api.market_data_stream import StreamCandle
from src.data.cache import MarketDataCache
from src.data.live_state import StreamClosureEvidence
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

NOW = datetime(2026, 10, 12, 12, 3, 30)
UID = "u"
ITEM = Instrument("SBER", "SBER", "share", "SBER", UID)


def minutes(*items):
    return pd.DataFrame([dict(datetime=pd.Timestamp(stamp), open=price, high=price + 1, low=price - 1,
                              close=price, volume=1, is_complete=True) for stamp, price in items])


def live_cache(tmp_path, *timeframes):
    clock = [NOW]
    timeline = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: clock[0])
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), UID), timeline,
                            request_budget=RequestBudget(lambda: clock[0], 0), market_store=store)
    for tf in timeframes or ("1m",):
        cache.register_live_pair(ITEM, UID, tf)
        cache.mark_live_history(UID, tf, done=True)
    return cache, store, clock


def trades_storage(timeframe="15m"):
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE trades(trade_id TEXT, plan_json TEXT)")
    connection.execute("INSERT INTO trades VALUES('t', ?)", (json.dumps({"timeframe": timeframe}),))
    return SimpleNamespace(connection=connection)


def test_dispatch_evidence_reflects_owner_readiness_and_intent_age(tmp_path):
    cache, _, _ = live_cache(tmp_path, "1m", "15m")
    since = [datetime(2026, 10, 12, 12, 3, tzinfo=timezone.utc)]
    inspect = run._dispatch_evidence(cache, [ITEM], trades_storage(), since)
    fresh = SimpleNamespace(trade_id="t", created_at=datetime(2026, 10, 12, 12, 3, 10, tzinfo=timezone.utc))
    held = SimpleNamespace(trade_id="t", created_at=datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc))
    evidence = inspect(UID, None, fresh)
    assert (evidence.execution_ready, evidence.work_ready, evidence.intent_current) == (True, True, True)
    assert inspect(UID, None, held).intent_current is None  # актуальность не доказана
    cache.set_live_gap(UID, "1m", {"state": "active"})
    assert inspect(UID, None, fresh).execution_ready is False
    assert inspect("unknown", None, fresh) is None


def runtime_for(cache, tmp_path):
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    storage = MagicMock()
    storage.load_trades.return_value = ()
    storage.execution_bar_boundaries.side_effect = lambda: {UID: (datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc), True)}
    with patch("run.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("run._load_contracts_metadata", return_value={}), patch("run.print_contract_metadata"), patch("run._risk_limits"):
        runtime = run._build_runtime([ITEM], cache, MagicMock(), tmp_path)
    return runtime, broker


def executed(broker):
    return [call.args[0].replace(tzinfo=None).minute for call in broker.track_bar.call_args_list]


def stream_candle(minute, price=100):
    return StreamCandle(UID, "1m", datetime(2026, 10, 12, 12, minute), datetime(2026, 10, 12, 12, minute, 20),
                        tuple(Decimal(value) for value in (price, price + 1, price - 1, price)), 1, 1)


def test_duplicate_stream_and_rpc_minute_is_executed_once(tmp_path):
    cache, store, _ = live_cache(tmp_path)
    cache.set_stream_evidence(UID, "1m", StreamClosureEvidence(UID, "1m", 1, 1, 1, True))
    runtime, broker = runtime_for(cache, tmp_path)
    cache.accept_stream_candle(stream_candle(0), epoch=1, received_at=NOW)
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100)), received_at=NOW)
    runtime.post_tick({"1m"})
    runtime.post_tick({"1m"})
    cache.accept_stream_candle(stream_candle(0), epoch=1, received_at=NOW)
    runtime.post_tick({"1m"})
    assert executed(broker) == [0]


def test_out_of_order_minutes_wait_for_missing_and_run_in_time_order_once(tmp_path):
    cache, store, _ = live_cache(tmp_path)
    runtime, broker = runtime_for(cache, tmp_path)
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100), ("2026-10-12 12:02", 102)), received_at=NOW)
    store.set_interval_state(source="tbank_exchange", instrument_uid=UID, interval="1m",
                             open_time="2026-10-12 12:01", state="pending", evidence="open_session_missing_candle")
    runtime.post_tick({"1m"})
    assert executed(broker) == [0]  # 12:02 не исполняется через неизвестную 12:01
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:01", 101)), received_at=NOW)
    runtime.post_tick({"1m"})
    runtime.post_tick({"1m"})
    assert executed(broker) == [0, 1, 2]


def test_first_late_arrival_is_not_a_correction(tmp_path):
    cache, store, _ = live_cache(tmp_path)
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100), ("2026-10-12 12:02", 102)), received_at=NOW)
    result = cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:01", 101)), received_at=NOW)
    assert result[0]["state"] == "accepted" and result[0]["changed"]
    assert cache.live_conflicts() == []


def test_integrity_hold_of_executed_minute_survives_restart_and_reconnect(tmp_path):
    cache, store, _ = live_cache(tmp_path)
    runtime, broker = runtime_for(cache, tmp_path)
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100)), received_at=NOW)
    runtime.post_tick({"1m"})
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 90)), received_at=NOW)
    assert cache.pair_status(ITEM, "1m")["state"] == "INTEGRITY_HOLD"
    cache.set_live_handoff(UID, "1m", transport="stream", epoch=2, ready=True)  # reconnect
    assert cache.pair_status(ITEM, "1m")["state"] == "INTEGRITY_HOLD"
    restarted, _, _ = live_cache(tmp_path)
    status = restarted.pair_status(ITEM, "1m")
    assert status["state"] == "INTEGRITY_HOLD" and status["incident"]["reason"] == "consumed_input_changed"
    assert executed(broker) == [0]  # ни компенсирующего, ни повторного исполнения


def test_effective_availability_is_not_before_input_publication(tmp_path):
    from src.bot import TradingBot
    from src.bot.live_analysis import LiveAnalysis
    from src.events import EventBus
    from src.strategies.contracts import Assignment, Decision, SignalType

    class Strategy:
        def required_history(self):
            return 1

        def compute(self, values):
            return values

        def decide(self, values, timeframe=""):
            return Decision(SignalType.BUY, float(values["close"].iloc[-1]))

    cache, store, clock = live_cache(tmp_path, "1m", "15m")
    late = datetime(2026, 10, 12, 12, 16, 40)
    clock[0] = datetime(2026, 10, 12, 12, 17)
    cache.accept_rpc_frame(UID, "15m", minutes(("2026-10-12 12:00", 100)), received_at=late)
    binding = Assignment(id="b", strategy="macd_rsi_stoch", management="levels_rr", filter_profile="raw", timeframe="15m")
    bot = TradingBot([ITEM], EventBus(), {"macd_rsi_stoch": object()}, cache, cache._timeline,
                     strategy_factory=lambda *a, **k: Strategy(), share_strategies={"SBER": [binding]})
    bot._validate()
    live = LiveAnalysis(bot)
    live.requirements[binding.id] = {"15m": 1}
    decision, *_ = live._calculate(ITEM, binding, cache.frame_for(ITEM, "15m"))
    assert decision.bar_time == datetime(2026, 10, 12, 12, 15)
    assert pd.Timestamp(decision.available_at) == pd.Timestamp(late)


def test_live_wait_wakes_on_data_and_is_bounded_by_deadline():
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: datetime(2026, 10, 12, 12, 0, 0))
    waited, sleeps = [], []
    timeline._sleep = sleeps.append
    timeline.wait_live_poll(5, data_waiter=waited.append, wake_at=datetime(2026, 10, 12, 12, 0, 2))
    assert waited == [2.0] and sleeps == []  # данные будят раньше, дедлайн ограничивает сон
    timeline.wait_live_poll(1)
    assert sleeps == [1.0]


def test_virtual_clock_ignores_live_wakeup():
    from src.scheduler.clock import HistoricalClock
    clock = HistoricalClock(datetime(2026, 10, 12), datetime(2026, 10, 13), step=timedelta(minutes=1))
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    waited = []
    timeline.wait_live_poll(1, data_waiter=waited.append)
    assert waited == []


def test_market_data_status_carries_ingestion_diagnostics_and_hold_reason(tmp_path):
    from src.bot import TradingBot
    from src.bot.live_analysis import LiveAnalysis
    from src.events import ALL_EVENT_TYPES, EventBus, EventType
    from src.notifier.templates import render
    from src.notifier.telegram_templates import render as telegram_render
    from src.strategies.contracts import Assignment, Decision, SignalType

    class Strategy:
        def required_history(self):
            return 1

        def compute(self, values):
            return values

        def decide(self, values, timeframe=""):
            return Decision(SignalType.HOLD, float(values["close"].iloc[-1]))

    cache, store, clock = live_cache(tmp_path, "1m", "15m")
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100)), received_at=NOW)
    cache.set_live_gap(UID, "1m", {"lower": "2026-10-12T12:01:00", "upper": "2026-10-12T12:02:00",
                                   "state": "parked", "attempts": 5, "next_retry_at": "2026-10-12T12:10:00",
                                   "reason": "expected_missing"})
    events = []

    class Channel:
        supported_types = ALL_EVENT_TYPES

        def handle(self, event):
            events.append(event)

    bus = EventBus()
    bus.subscribe(Channel())
    binding = Assignment(id="b", strategy="macd_rsi_stoch", management="levels_rr", filter_profile="raw", timeframe="15m")
    bot = TradingBot([ITEM], bus, {"macd_rsi_stoch": object()}, cache, cache._timeline,
                     strategy_factory=lambda *a, **k: Strategy(), share_strategies={"SBER": [binding]})
    bot._validate()
    live = LiveAnalysis(bot)
    live.requirements[binding.id] = {"15m": 1}
    from src.data.live_state import LivePairState
    live.states[live._key(ITEM, "1m")] = LivePairState("tbank_exchange", UID, "1m", required_bars=1)
    live.publish_status(ITEM, "1m", cache.frame_for(ITEM, "1m"))
    status = next(event for event in events if event.type is EventType.MARKET_DATA_STATUS)
    ingestion = status.get("ingestion")
    assert ingestion["transport"] == "polling" and ingestion["data_hold"] == "GAP_HOLD"
    assert ingestion["gap"]["state"] == "parked" and ingestion.get("processed_through") is None
    assert "не восстановлен" in status.get("reason")
    assert "Источник данных: опрос · удержание: пропуск" in render(status, 3)
    assert "удержание: пропуск" in telegram_render(status, tz_offset_hours=3)
    assert "token" not in str(status.to_dict()).lower()


def test_new_high_water_does_not_clear_deadline_of_internal_gap():
    from src.data.live_state import LivePairState
    close = datetime(2026, 10, 12, 12, 2)
    state = LivePairState("s", UID, "1m").observe(now=close, session="open", expected_bar=close, timeout_secs=60)
    later = state.observe(now=close + timedelta(minutes=3), session="open", expected_bar=close + timedelta(minutes=2),
                          timeout_secs=60, received_bar=close + timedelta(minutes=2), resolved_bar=close - timedelta(minutes=1))
    assert later.expected_bar == close and later.publication_deadline == close + timedelta(seconds=60)
    assert later.data_state == "delayed"
