"""Кэш контура на ленте ядра (add-contour-market-feed): снимок, лента, готовность из базы."""

import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.assembly import contour
from src.data.cache import MarketDataCache
from src.data.follower import FeedReader, FollowerMarketDataCache
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler
from src.data.request_budget import RequestBudget
from src.events import ALL_EVENT_TYPES, EventType
from src.instruments import Instrument
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies.contracts import Assignment, Decision, SignalType

NOW = datetime(2026, 10, 12, 12, 3, 30)
A = Instrument("AAA", "AAA", "share", "AAA", "aaa")
B = Instrument("BBB", "BBB", "share", "BBB", "bbb")


def bars(start, count, price=100):
    stamps = pd.date_range(pd.Timestamp(start), periods=count, freq="1min")
    return pd.DataFrame({"datetime": stamps, "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1, "is_complete": True})


class Link:
    """Связь с ядром в тесте: запросы контура передаются кэшу ядра и запоминаются."""

    def __init__(self, core):
        self.core, self.requests, self.touched = core, [], []

    def request_history(self, instrument, timeframe, min_bars):
        self.requests.append((instrument.ticker, timeframe, min_bars))
        if self.core is not None:
            self.core.request_live_history(instrument, timeframe, min_bars)

    def touch(self, instrument, timeframe):
        self.touched.append((instrument.ticker, timeframe))
        if self.core is not None:
            self.core.frame_for(instrument, timeframe)


def core_cache(tmp_path):
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: NOW)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), kw["instrument_id"]), timeline,
                            request_budget=RequestBudget(lambda: NOW, 0), market_store=store)
    cache.contour_id = "emulation"
    return timeline, store, cache


def follower(timeline, store, core=None, *, reconciler=True):
    link = Link(core)
    rec = MarketDataReconciler(store, source="tbank_exchange", read_only=True) if reconciler else None
    cache = FollowerMarketDataCache(timeline, store, core=link, market_reconciler=rec,
                                    request_budget=RequestBudget(lambda: NOW, 0))
    cache.contour_id = "emulation"
    reader = FeedReader(cache, store, consumer_id="contour-emulation")
    reader._open()
    return cache, reader, link


def healthy_and_gap(tmp_path):
    """Два UID: у AAA неизвестна 12:01, BBB здоров (как в приёмке reliable-live-candle-ingestion)."""
    timeline, store, cache = core_cache(tmp_path)
    for item in (A, B):
        cache.note_execution_lower(item, pd.Timestamp("2026-10-12 12:00"))
        cache.register_live_pair(item, item.uid, "1m")
        cache.mark_live_history(item.uid, "1m", done=True)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:00", 1).iloc[[0]], received_at=NOW)
    cache.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:02", 1), received_at=NOW)
    store.set_interval_state(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                             open_time="2026-10-12 12:01", state="pending", evidence="open_session_missing_candle")
    cache.accept_rpc_frame("bbb", "1m", bars("2026-10-12 12:00", 3), received_at=NOW)
    return timeline, store, cache


def executed_minutes(tmp_path, cache):
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    storage = MagicMock()
    storage.load_trades.return_value = ()
    start = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    storage.execution_bar_boundaries.side_effect = lambda: {"aaa": (start, True), "bbb": (start, True)}
    with patch("src.assembly.contour.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch(
        "src.assembly.contour.print_contract_metadata"), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([A, B], cache, MagicMock(), tmp_path / "state")
    assert runtime.post_tick({"1m"}) is True
    executed = {}
    for call in broker.track_bar.call_args_list:
        for uid in call.args[1]:
            executed.setdefault(uid, []).append(call.args[0].minute)
    return executed


def test_decisions_on_feed_match_core_memory(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path / "core")
    expected = executed_minutes(tmp_path / "core", core)

    timeline, store, core = healthy_and_gap(tmp_path / "feed")
    cache, reader, _ = follower(timeline, store, core)
    for item in (A, B):
        cache.want(item, "1m")
    reader.poll_once()
    assert executed_minutes(tmp_path / "feed", cache) == expected == {"aaa": [0], "bbb": [0, 1, 2]}
    assert cache.execution_ready(B, pd.Timestamp("2026-10-12 12:02"))
    assert not cache.execution_ready(A, pd.Timestamp("2026-10-12 12:02"))
    # Отметки контура в общей базе, ядро ничего не ставило под своим именем.
    assert store.cursors(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         contour="emulation")["processed_through"] == "2026-10-12T12:02:00Z"


def test_frame_and_status_match_core(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    pd.testing.assert_frame_equal(cache.frame_for(B, "1m").reset_index(drop=True),
                                  core.frame_for(B, "1m").reset_index(drop=True), check_dtype=False, check_like=True)
    mine, theirs = cache.pair_status(B, "1m"), core.pair_status(B, "1m")
    for name in ("state", "uid", "transport", "history", "history_error", "gap", "received_through",
                 "resolved_through", "cutover", "lanes"):
        assert mine[name] == theirs[name], name
    assert cache.coverage_for(B, "1m", cache.frame_for(B, "1m")) == core.coverage_for(B, "1m", core.frame_for(B, "1m"))


def test_change_between_cursor_and_snapshot_keeps_one_latest_candle(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    floor = store.source_info().max_change_id
    # Исправление пришло после фиксации края, но до чтения снимка.
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:02", 1, price=105))
    frame = store.current_frame(source="tbank_exchange", instrument_uid="bbb", interval="1m")
    cache.bind(cache._key(B, "1m"), "bbb", "1m", frame, floor=floor,
               row=next(row for row in store.pair_readiness_all(source="tbank_exchange") if row["instrument_uid"] == "bbb"))
    reader.poll_once()
    closed = cache.frame_for(B, "1m")
    assert list(closed["datetime"]).count(pd.Timestamp("2026-10-12 12:02")) == 1
    assert closed["close"].iloc[-1] == 105


def test_new_candles_arrive_by_feed_and_raise_generation(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    generation = cache.data_generation
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:03", 1, price=110))
    reader.poll_once()
    assert cache.data_generation > generation
    assert reader.applied == store.source_info().max_change_id


def test_foreign_pairs_are_skipped_but_cursor_passes_them(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    store.record_candles(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                         candles=bars("2026-10-12 12:03", 1))
    reader.poll_once()
    assert cache.frame_for(A, "1m").empty
    assert reader.applied == store.source_info().max_change_id


def test_unknown_pair_goes_to_core_without_network(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, link = follower(timeline, store, core)
    late = Instrument("CCC", "CCC", "share", "CCC", "ccc")
    assert cache.frame_for(late, "1h").empty
    assert cache.prepare_live_history(late, "1h", 50).empty
    reader.poll_once()
    assert link.touched == [("CCC", "1h")] and link.requests == [("CCC", "1h", 50)]
    assert cache.pair_status(late, "1h")["state"] == "BOOTSTRAPPING"
    # Потребность дошла до ядра, сеть контур не трогал (loader запрещает её).
    assert ("CCC", "1h", 50) in [(item.ticker, key[2], count) for key, item, count in core.live_needs()]


def test_pair_without_card_uid_binds_by_ticker(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    selected = Instrument("BBB", "BBB", "share", "BBB")            # live-выбор без UID
    cache.want(selected, "1m")
    reader.poll_once()
    assert cache.pair_uid(selected, "1m") == "bbb"
    assert len(cache.frame_for(selected, "1m")) == 3


def test_cursor_expired_rebuilds_from_snapshot(tmp_path, caplog):
    timeline, store, core = healthy_and_gap(tmp_path)
    store.acknowledge("contour-emulation", store.producer_id, 1)
    store.acknowledge("contour-emulation", store.producer_id, 1)
    with store._connect() as conn:                                   # очистка ушла дальше курсора
        conn.execute("UPDATE metadata SET value='3' WHERE key='min_after_id'")
    with caplog.at_level(logging.WARNING, logger="src.data.follower"):
        cache, reader, _ = follower(timeline, store, core)
    assert any("устарел" in record.getMessage() for record in caplog.records)
    cache.want(B, "1m")
    reader.poll_once()
    assert len(cache.frame_for(B, "1m")) == 3
    assert reader.applied == store.source_info().max_change_id


def test_cursor_expired_while_running_rebuilds(tmp_path, caplog):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    reader._cursor = 0
    with store._connect() as conn:
        conn.execute("UPDATE metadata SET value='2' WHERE key='min_after_id'")
    with caplog.at_level(logging.WARNING, logger="src.data.follower"):
        reader.poll_once()
    assert any("пересобирается" in record.getMessage() for record in caplog.records)
    reader.poll_once()
    assert len(cache.frame_for(B, "1m")) == 3


def test_restart_continues_from_acknowledged_cursor(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    reader.acknowledge()
    acknowledged = reader.applied
    store.record_candles(source="tbank_exchange", instrument_uid="bbb", interval="1m",
                         candles=bars("2026-10-12 12:03", 1))
    again, second, _ = follower(timeline, store, core)
    assert second._cursor == acknowledged
    again.want(B, "1m")
    second.poll_once()
    assert len(again._frames[again._key(B, "1m")]) == 4 and second.applied == store.source_info().max_change_id
    # Очистка ленты держится за курсор контура.
    assert store.checkpoint("contour-emulation")["acknowledged_through"] == acknowledged


def test_pair_removed_by_core_is_unbound(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(B, "1m")
    reader.poll_once()
    core.unregister_live_pair("bbb", "1m")
    reader.poll_once()
    assert cache.pair_status(B, "1m")["state"] == "BOOTSTRAPPING"


def test_late_candle_after_source_absent_is_reported(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    cache.want(A, "1m")
    reader.poll_once()
    store.set_interval_state(source="tbank_exchange", instrument_uid="aaa", interval="1m",
                             open_time="2026-10-12 12:01", state="source_absent", evidence="source_absent:checks")
    reader.poll_once()
    core.accept_rpc_frame("aaa", "1m", bars("2026-10-12 12:01", 1, price=99), received_at=NOW)
    reader.poll_once()
    assert [(uid, stamp) for uid, stamp, _ in cache.drain_late_source_absent()] == [
        ("aaa", pd.Timestamp("2026-10-12 12:01"))]


def test_follower_never_writes_market_state(tmp_path):
    timeline, store, core = healthy_and_gap(tmp_path)
    cache, reader, _ = follower(timeline, store, core)
    for item in (A, B):
        cache.want(item, "1m")
    reader.poll_once()
    before = store.source_info().max_change_id
    readiness = store.pair_readiness_all(source="tbank_exchange")
    cache.coverage_for(A, "1m", cache.frame_for(A, "1m"))
    cache.pair_status(A, "1m")
    assert store.source_info().max_change_id == before
    assert store.pair_readiness_all(source="tbank_exchange") == readiness
    with pytest.raises(RuntimeError):
        cache._market_reconciler.record_empty_recovery(instrument_uid="aaa", interval="1m", open_time=NOW)


def test_core_publishes_readiness_once_per_state_and_throttles_lanes(tmp_path, monkeypatch):
    timeline, store, core = core_cache(tmp_path)
    writes = []
    original = store.publish_pair_readiness
    monkeypatch.setattr(store, "publish_pair_readiness", lambda **kw: writes.append(kw["status"]["state"]) or original(**kw))
    core.register_live_pair(B, "bbb", "1m")
    core.mark_live_history("bbb", "1m", done=True)
    assert writes == ["BOOTSTRAPPING", "READY"]
    core.pair_status(B, "1m")                                  # чтение не пишет повторно
    core.set_lane_status("bbb", "1m", "evidence", {"checked_at": "12:00"})
    core.set_lane_status("bbb", "1m", "evidence", {"checked_at": "12:01"})
    assert writes == ["BOOTSTRAPPING", "READY"]                # сводка полос — не чаще READINESS_LANES_SECONDS
    row = store.pair_readiness(source="tbank_exchange", instrument_uid="bbb", interval="1m")
    assert row["state"] == "READY" and row["ticker"] == "BBB" and row["instrument_type"] == "share"


def test_core_start_drops_previous_readiness(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.assembly import core as assembly_core

    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    store.publish_pair_readiness(source="tbank_exchange", instrument_uid="bbb", interval="1m", status={"state": "READY"})
    monkeypatch.setattr(assembly_core, "MARKET_DATA_DATABASE_FILE", "market-data.sqlite3")
    provider = SimpleNamespace(calendar_source=lambda *a, **k: None)
    assembly_core._build_market_data(state_dir=tmp_path, clock=None, provider=provider, history=False)
    assert store.pair_readiness_all(source="tbank_exchange") == []


# ── Весь путь контура на ленте ядра (add-contour-process, задача 5.1) ──
#
# «Ядро» — настоящий кэш ядра над рыночной базой с сохранённым расписанием:
# принимает заготовленные свечи и сам прогревает запрошенные пары. Контур
# собирается как в процессе (`TradingContour` на `FeedMarketServices`) и
# сверяется с П3.1 — тем же контуром на памяти ядра. Подставлены только
# стратегия («покупать на 102»), уровни контекста и приём свечей вместо сети.

SBER = Instrument("SBER", "SBER", "share", "SBER", "uid-sber")
SCHEDULE = Path(__file__).parents[1] / "market_calendar" / "fixtures" / "trading_schedules_20261013.json"
SHARES_EXCHANGE = "moex_mrng_evng_e_wknd_dlr"
OPENED = datetime(2026, 10, 13, 8, 0)               # 11:00 МСК, основная сессия
WARMUP = 60
FLAT = (100, 101, 99, 100)
# Минуты после прогрева (O, H, L, C): сигнал на 102 (09:01), лимитный вход 102 исполняется 09:03, цель 120 — 09:05.
SCRIPT = [FLAT, (102, 102, 101, 102), (102, 103, 102, 103), (103, 104, 101, 103),
          (105, 110, 104, 109), (110, 121, 109, 120), (120, 121, 119, 120), (120, 121, 119, 120)]
ENTRY_FILLED = 5                                    # шагов контура до открытой сделки
BINDING = {"SBER": [Assignment("sber-1m", "macd_rsi_stoch", "levels_rr", "raw", timeframe="1m")]}
PROFILES = {"levels_rr": {"buffer_ticks": 1, "target_R": [2.0], "shares": [1.0], "max_adds": 0,
                          "min_stop_atr": 0, "min_stop_ticks": 1, "stop_beyond_bar": 0, "max_stop_atr": None}}
NOISE = {EventType.MARKET_DATA_STATUS, EventType.TICK_STARTED, EventType.TICK_FINISHED, EventType.HEARTBEAT,
         EventType.STARTUP_ANALYSIS}
FILLS = [("OPEN", "102.0", "2026-10-13T09:03:00+00:00"), ("TARGET:tp-1", "120", "2026-10-13T09:05:00+00:00")]


class BuyOn102:
    def required_history(self):
        return 10

    def compute(self, frame):
        return frame

    def decide(self, frame, timeframe=None):
        close = float(frame["close"].iloc[-1])
        return Decision(SignalType.BUY if close == 102 else SignalType.HOLD, close)


class Levels:
    """Контекст с поддержкой 95: стоп 94, цель 2R — 120."""

    def __init__(self, **_):
        pass

    def context_for_frame(self, instrument, timeframe, frame):
        return MarketContext(TrendResult(TrendDirection.UP, .8), [SRLevel(95, SRType.SUPPORT, 3, "support")],
                             float(frame["close"].iloc[-1]))


def _candles(first, last):
    rows = [(OPENED + timedelta(minutes=minute), *(FLAT if minute < WARMUP else SCRIPT[minute - WARMUP]))
            for minute in range(first, last + 1)]
    return pd.DataFrame([dict(datetime=pd.Timestamp(stamp), open=o, high=h, low=lo, close=c, volume=10,
                              is_complete=True) for stamp, o, h, lo, c in rows])


def _trading_day():
    from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

    def moment(value):
        return datetime.fromisoformat(value).astimezone(timezone.utc) if isinstance(value, str) else value
    entry = json.loads(SCHEDULE.read_text(encoding="utf-8"))[SHARES_EXCHANGE]["days"]["2026-10-13"]
    intervals = [TradingInterval(type=item["type"], interval=TimeInterval(
        start_ts=moment(item["start"]), end_ts=moment(item["end"]))) for item in entry["intervals"]]
    return TradingDay(**{name: moment(value) for name, value in entry["named_fields"].items()}, intervals=intervals)


class Core:
    """Ядро в тесте: кэш ядра над рыночной базой; свечи по сценарию вместо сети."""

    def __init__(self, path, clock):
        from src.market_calendar.service import MarketCalendar, StoredScheduleSource

        self.clock = clock
        self.store = MarketDataStore(path / "market-data.sqlite3")
        self.store.record_schedule_binding(source="tbank", instrument_uid=SBER.uid, exchange=SHARES_EXCHANGE,
                                           observed_at=clock[0])
        self.store.record_schedule_day(source="tbank", exchange=SHARES_EXCHANGE, day=_trading_day(),
                                       observed_at=clock[0])
        self.timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0])
        calendar = MarketCalendar(self.store, StoredScheduleSource(), schedule_max_age_secs=129600)
        self.cache = MarketDataCache(lambda **kw: pytest.fail("ядро в тесте не ходит в сеть"), self.timeline,
                                     request_budget=RequestBudget(lambda: clock[0], 0), market_store=self.store,
                                     market_reconciler=MarketDataReconciler(self.store, source="tbank_exchange",
                                                                            market_calendar=calendar))
        self.received = 0

    def receive(self):
        """Закрытые свечи до текущей минуты; пары, которые запросили, прогреваются всей историей."""
        last = int((self.clock[0] - OPENED).total_seconds() // 60) - 1
        if last >= self.received:
            for pair in self.cache.live_pairs():
                self.cache.accept_rpc_frame(pair["uid"], "1m", _candles(self.received, last), received_at=self.clock[0])
            self.received = last + 1
        for key, instrument, min_bars in self.cache.live_needs():
            self.cache.register_live_pair(instrument, SBER.uid, key[2], min_bars)
            self.cache.accept_rpc_frame(SBER.uid, key[2], _candles(0, last), received_at=self.clock[0])
            self.cache.mark_live_history(SBER.uid, key[2], done=True)
            self.cache.finish_live_need(key, min_bars)


def _patch_contour(monkeypatch):
    from src.assembly import feed
    from src.strategies import registry

    registry._discover_strategies()
    monkeypatch.setitem(registry._registry, "macd_rsi_stoch", lambda config=None: BuyOn102())
    monkeypatch.setattr(contour, "trading_enabled", lambda: True)
    monkeypatch.setattr(contour, "SHARE_STRATEGIES", BINDING)
    monkeypatch.setattr(contour, "FUTURE_STRATEGIES", {})
    monkeypatch.setattr(contour, "RISK_LIMITS", {"max_qty": 1, "commission": 1, "slippage": 0})
    monkeypatch.setattr(contour, "TRADE_MANAGEMENT_PROFILES", PROFILES)
    monkeypatch.setattr("src.bot.live_analysis.TRADE_MANAGEMENT_PROFILES", PROFILES)
    monkeypatch.setattr(contour, "MarketContextCache", Levels)
    monkeypatch.setattr(contour, "print_contract_metadata", lambda *a: None)
    monkeypatch.setattr(feed, "CLUSTERS_ENABLED", False)
    monkeypatch.setattr(feed, "MARKET_DATA_SETTINGS",
                        dataclasses.replace(feed.MARKET_DATA_SETTINGS, startup_wait_timeout_secs=5))


def _run_contour(path, monkeypatch, *, feed, first=0, last=len(SCRIPT), before_contour=None):
    """Шаги ``first..last`` сценария: контур процессом на ленте (``feed``) или П3.1 на памяти ядра.

    Шаг — минута рыночных часов: ядро принимает закрытую свечу, контур делает тик.
    Возвращает ядро, торговые события, исполнения журнала и обязательства, которые видело ядро.
    """
    from src.assembly.contour import ContourSettings, TradingContour
    from src.assembly.demand import ContourDemandPoller
    from src.assembly.feed import FeedMarketServices
    from src.bot.run_session import LiveRunSession
    from src.data.timeutil import to_naive
    from src.portfolio import ContractMeta
    from src.scheduler.clock import FunctionClock

    _patch_contour(monkeypatch)
    clock = [OPENED + timedelta(minutes=WARMUP + first, seconds=5)]
    core = Core(path, clock)
    events, obligations = [], []
    recorder = SimpleNamespace(supported_types=ALL_EVENT_TYPES, handle=events.append, close=lambda: None)
    monkeypatch.setattr(contour, "build_channels", lambda names, state_dir=None: [recorder])
    if feed:
        poller = ContourDemandPoller(core.store, core.cache, instruments=[SBER], tracks_obligations=True)
        if before_contour is not None:
            before_contour(core, poller)
        services = FeedMarketServices(core.store, contour_id="emulation", data_dir=path, tracks_obligations=True,
                                      poll_seconds=0.01, timeline=MultiTimeframeScheduler(["1m"], clock=lambda: clock[0]))
    else:
        poller = None
        services = SimpleNamespace(data_cache=core.cache, timeline=core.timeline, tracks_obligations=True,
                                   cluster_gate=lambda state_dir: None, acknowledge_tick=lambda: None,
                                   # Как строки базы: границы naive UTC.
                                   update_obligations=lambda value: obligations.append(
                                       {key: (to_naive(stamp), inclusive) for key, (stamp, inclusive) in value.items()}),
                                   close=lambda: None)

    def sync():
        """Ядро применяет потребности контура и принимает свечи; контур догоняет ленту."""
        if poller is not None:
            poller.poll_once()
            obligations.append(poller.obligations())
        core.receive()
        if poller is not None and services.reader._thread.is_alive():
            poller.poll_once()
            assert services.reader.wait_applied(core.store.source_info().max_change_id, 5)
            assert services.reader.wait_cycles(1, 5)

    step = [first]

    class Session(LiveRunSession):
        def should_stop(self):
            return step[0] >= last

    def next_minute(*args, **kwargs):
        step[0] += 1
        clock[0] += timedelta(minutes=1)
        sync()

    services.timeline.wait_live_poll = next_minute
    built = TradingContour(ContourSettings(
        kind="emulation", state_dir=path / "contours" / "emulation", channel_names=None,
        clock=FunctionClock(lambda: clock[0]), session=Session(), client_provider=None, instruments=[SBER],
        contracts={SBER.uid: ContractMeta(SBER.uid, 1, 1, 0, 0)}), services)
    sync()
    try:
        built.build()
        if feed:
            ready = services.serve(built.demand())

            def waiter():
                sync()
                return ready()
        else:
            waiter = sync
        built.run(waiter)
        if poller is not None:
            poller.poll_once()
            obligations.append(poller.obligations())
        connection = built.runtime.storage.connection
        fills = [tuple(row) for row in connection.execute(
            "SELECT o.action_type, f.price, f.executed_at FROM fills f JOIN orders o USING(order_id) ORDER BY f.rowid")]
        phases = [row[0] for row in connection.execute("SELECT phase FROM trades")]
    finally:
        services.close()
        built.close()
        if built.runtime is not None and built.runtime.storage is not None:
            built.runtime.storage.close()
    trading = [(event.type, event.bar_time) for event in events if event.type not in NOISE]
    return core, trading, (fills, phases), [value for i, value in enumerate(obligations)
                                           if i == 0 or value != obligations[i - 1]]


def test_contour_process_on_feed_matches_core_memory(tmp_path, monkeypatch):
    _, expected, journal, lowers = _run_contour(tmp_path / "memory", monkeypatch, feed=False)
    core, trading, fills, obligations = _run_contour(tmp_path / "feed", monkeypatch, feed=True)

    # Сигналы, план, вход и выход — те же, что у контура на памяти ядра (П3.1).
    assert trading == expected
    assert [kind for kind, _ in trading if kind is not EventType.DECISION] == [
        EventType.SIGNAL, EventType.TRADE_OPENED, EventType.TARGET_HIT]
    assert fills == journal == (FILLS, ["CLOSED"])
    # Обязательства сделки дошли до ядра строками contour_obligations и сняты после выхода.
    entry, held = {SBER.uid: (datetime(2026, 10, 13, 9, 1), True)}, {SBER.uid: (datetime(2026, 10, 13, 9, 3), False)}
    assert entry in obligations and held in obligations and obligations[-1] == {}
    assert [value for value in obligations if value] == [value for value in lowers if value]
    assert core.store.contour_obligations() == {}
    # Спрос контура — строкой в базе, прогрел пару ядро.
    assert [(row["contour_id"], row["instrument_key"], row["interval"]) for row in core.store.contour_demands()] == [
        ("emulation", SBER.uid, "1m")]
    # Курсор ленты подтверждается только после тика: последнюю свечу тик уже не обработал.
    assert 0 < core.store.checkpoint("contour-emulation")["acknowledged_through"] < core.store.source_info().max_change_id


def test_core_restart_applies_obligations_before_contour_starts(tmp_path, monkeypatch):
    _, _, (fills, phases), _ = _run_contour(tmp_path, monkeypatch, feed=True, last=ENTRY_FILLED)
    assert fills == FILLS[:1] and phases == ["OPEN"]
    seen = {}

    def restarted(core, poller):
        # Новое ядро: процесса контура ещё нет, обязательства — из строк прошлого запуска.
        seen["rows"] = poller.obligations()
        poller.poll_once()
        core.receive()
        seen["lower"] = core.cache._live_members[SBER.uid, "1m"]["execution_lower"]

    _, trading, (fills, phases), _ = _run_contour(tmp_path, monkeypatch, feed=True, first=ENTRY_FILLED,
                                                  before_contour=restarted)
    # Снимок пишется до исполнения тика: граница 09:01 включительно — раньше входа, сделка под защитой.
    boundary = datetime(2026, 10, 13, 9, 1)
    assert seen == {"rows": {SBER.uid: (boundary, True)}, "lower": boundary}
    # Сопровождение продолжилось после перезапуска: цель взята, сделка закрыта.
    assert EventType.TARGET_HIT in [kind for kind, _ in trading]
    assert fills == FILLS and phases == ["CLOSED"]
