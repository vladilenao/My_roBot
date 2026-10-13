"""Исполнение через минуту «у источника нет» и поздняя свеча после неё (группа 7)."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

SOURCE = "tbank_exchange"
UID = "uid-ng"
ABSENT = pd.Timestamp("2026-10-13 11:20")
EVIDENCE = "source_absent:checks=3:first=2026-10-13T11:25:00:last=2026-10-14T11:25:00"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _candles(*times, low=99, high=101):
    return pd.DataFrame({
        "datetime": pd.to_datetime(times), "open": [100] * len(times),
        "high": [high] * len(times), "low": [low] * len(times),
        "close": [100] * len(times), "volume": [1] * len(times),
        "is_complete": [True] * len(times),
    })


def _cache(tmp_path, state="source_absent"):
    clock = [datetime(2026, 10, 14, 12, 0)]
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m",
                         candles=_candles("2026-10-13 11:19", "2026-10-13 11:21"))
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time=ABSENT,
                             state=state, evidence=EVIDENCE if state == "source_absent" else "pending")
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0])
    cache = MarketDataCache(lambda **kwargs: (_candles(), UID), timeline,
                            request_budget=RequestBudget(lambda: clock[0], 0), market_store=store)
    item = Instrument("NG", "NGV6", "future", "NG-10.26", UID)
    cache.register_live_pair(item, UID, "1m")
    cache.mark_live_history(UID, "1m", done=True)
    IngestionWorker(cache)
    return cache, store, item


def test_prefix_and_barrier_pass_source_absent_minute(tmp_path):
    cache, store, item = _cache(tmp_path)
    fresh = _candles("2026-10-13 11:21")

    assert len(cache.contiguous_after(item, "1m", fresh, pd.Timestamp("2026-10-13 11:19"))) == 1
    assert store.resolve_contiguous_prefix(source=SOURCE, instrument_uid=UID, interval="1m",
                                           meaningful_lower="2026-10-13 11:19",
                                           horizon="2026-10-13 11:21") == pd.Timestamp("2026-10-13 11:21")
    assert cache.source_absent_between(item, pd.Timestamp("2026-10-13 11:19"), fresh) == [(ABSENT, EVIDENCE)]


def test_unresolved_minute_still_holds_barrier(tmp_path):
    cache, _, item = _cache(tmp_path, state="unresolved")
    fresh = _candles("2026-10-13 11:21")

    assert cache.contiguous_after(item, "1m", fresh, pd.Timestamp("2026-10-13 11:19")).empty
    assert cache.source_absent_between(item, pd.Timestamp("2026-10-13 11:19"), fresh) == []


def test_late_candle_after_source_absent_is_queued_for_journal(tmp_path):
    cache, store, _ = _cache(tmp_path)

    cache.accept_rpc_frame(UID, "1m", _candles(ABSENT, low=95), received_at=datetime(2026, 10, 14, 12, 1))

    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time=ABSENT) == "received"
    assert cache.drain_late_source_absent() == [(UID, ABSENT, (100.0, 101.0, 95.0, 100.0))]
    assert cache.drain_late_source_absent() == []


class _FillingBroker(BrokerPort):
    def contract_for(self, ticker):
        return None

    def submit(self, action, now):
        return ExecutionEvent(f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
                              ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason)


def _opened(storage):
    manager = TradeManager(storage, _FillingBroker(), initial_balance=Decimal("1000"))
    plan = TradePlan("trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
                     (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
                     ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW)
    manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
    manager.dispatch(NOW)
    return manager


def _events(storage, kind):
    return [(row[0], row[1]) for row in storage.connection.execute(
        "SELECT event_id, payload_json FROM events WHERE event_type=? ORDER BY event_seq", (kind,))]


def _facts(storage):
    connection = storage.connection
    return (connection.execute("SELECT * FROM positions").fetchall(),
            connection.execute("SELECT * FROM fills").fetchall(),
            connection.execute("SELECT * FROM protection").fetchall(),
            connection.execute("SELECT phase, state_revision FROM trades").fetchall())


def test_source_absent_mark_is_durable_and_idempotent_across_restart(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        manager = _opened(storage)
        assert manager.record_source_absent("NGV6", [(ABSENT, EVIDENCE)], observed_at=NOW) == 1
    # Перезапуск между пометкой и продвижением курсора: минута проходит снова.
    with Storage(path) as storage:
        manager = TradeManager(storage, _FillingBroker(), initial_balance=Decimal("1000"))
        assert manager.record_source_absent("NGV6", [(ABSENT, EVIDENCE)], observed_at=NOW) == 0
        events = _events(storage, "MARKET_DATA_SOURCE_ABSENT")
    assert [event_id for event_id, _ in events] == ["source-absent:trade-1:2026-10-13T11:20:00"]
    payload = json.loads(events[0][1])
    assert payload["evidence"] == EVIDENCE and payload["side"] == "BUY" and payload["targets"] == ["104"]


def test_late_candle_flags_touched_stop_without_changing_facts(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage)
        manager.record_source_absent("NGV6", [(ABSENT, EVIDENCE)], observed_at=NOW)
        assert storage.connection.execute("SELECT confirmed_stop FROM protection").fetchone()[0] == "96"
        before = _facts(storage)

        assert manager.record_late_candle("NGV6", ABSENT, (100.0, 101.0, 95.0, 100.0), observed_at=NOW) == 1
        assert manager.record_late_candle("NGV6", ABSENT, (100.0, 101.0, 95.0, 100.0), observed_at=NOW) == 0

        assert _facts(storage) == before
        (event_id, payload_json), = _events(storage, "MARKET_DATA_LATE_CANDLE")
    payload = json.loads(payload_json)
    assert event_id == "late-candle:trade-1:2026-10-13T11:20:00"
    assert payload["touched_stop"] is True
    assert payload["touched_target"] is False


def test_late_candle_without_skipped_minute_is_ignored(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage)
        assert manager.record_late_candle("NGV6", ABSENT, (100.0, 105.0, 99.0, 100.0), observed_at=NOW) == 0
        assert _events(storage, "MARKET_DATA_LATE_CANDLE") == []


def test_trade_journaled_under_ticker_alias_is_marked(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage)
        assert manager.record_source_absent(["uid-ng", "NGV6"], [(ABSENT, EVIDENCE)], observed_at=NOW) == 1
        assert manager.record_late_candle(["uid-ng", "NGV6"], ABSENT, (100.0, 105.0, 99.0, 100.0),
                                          observed_at=NOW) == 1
        (_, payload_json), = _events(storage, "MARKET_DATA_LATE_CANDLE")
    payload = json.loads(payload_json)
    assert payload["touched_target"] is True and payload["touched_stop"] is False
