from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import io
import tempfile
from pathlib import Path

import pandas as pd

from src.bot import TradingBot
from src.bot.live_analysis import LiveAnalysis
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.request_budget import RequestBudget
from src.data.reconciliation import SessionCalendar
from src.data.reconciliation import MarketDataReconciler
from src.data.market_store import MarketDataStore
from src.trade_management.models import RejectionReason, SignalAdmission
from src.events import EventBus, EventType, ALL_EVENT_TYPES
from src.instruments import Instrument
from src.notifier.console import ConsoleChannel
from src.notifier.templates import render
from src.notifier.telegram_templates import render as telegram_render
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies.contracts import Assignment, Decision, SignalType

# Прежняя общая очередь — откат при independent_lanes = false.
LEGACY_QUEUE = MarketDataSettings(independent_lanes=False)


def frame(end, tf="1h", count=100):
    stamps = pd.date_range(end=pd.Timestamp(end), periods=count, freq="h" if tf == "1h" else "15min" if tf == "15m" else "min")
    return pd.DataFrame({"datetime": stamps, "open": [100]*count, "high": [101]*count,
        "low": [99]*count, "close": [100]*count, "volume": [1]*count, "is_complete": [True]*count})


class Strategy:
    def required_history(self):
        return 10
    def compute(self, values):
        return values.assign(rsi=50)
    def decide(self, values, timeframe=""):
        return Decision(SignalType.BUY, float(values["close"].iloc[-1]))


def setup(now=datetime(2026, 10, 12, 6, 30), *, end="2026-10-09 19:00", manager=None, cutover=None):
    clock = [now]
    timeline = MultiTimeframeScheduler(["1h", "15m", "1m"], clock=lambda: clock[0])
    data = {(name, tf): frame(end, tf) for name in ("SBER", "GAZP") for tf in ("1h", "15m", "1m")}
    def loader(**kwargs):
        return data[kwargs["ticker"], kwargs["timeframe"]], kwargs["ticker"]
    store = MarketDataStore(Path(tempfile.mkdtemp()) / "market.sqlite3")
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0), market_store=store,
                            live_cutover=cutover)
    worker = IngestionWorker(cache, settings=LEGACY_QUEUE)
    events = []
    class Channel:
        supported_types = ALL_EVENT_TYPES
        def handle(self, event):
            events.append(event)
    bus = EventBus()
    bus.subscribe(Channel())
    bindings = {name: [Assignment(id=f"{name}-{tf}", strategy="macd_rsi_stoch", management="levels_rr", filter_profile="raw", timeframe=tf)
        for tf in ("1h", "15m")] for name in ("SBER", "GAZP")}
    bot = TradingBot([Instrument(name, name, "share", name, name) for name in bindings], bus,
        {"macd_rsi_stoch": object()}, cache, timeline, strategy_factory=lambda *args, **kw: Strategy(),
        share_strategies=bindings, trade_manager=manager, post_tick=Mock(return_value=True), heartbeat_every_ticks=2,
        live_data_waiter=worker.run_until_idle)
    bot._validate()
    live = LiveAnalysis(bot)
    # Фоновая ingestion-полоса в тесте выполняется синхронно.
    live.worker = worker
    return live, cache, data, events, clock


def test_friday_overview_is_immediate_dated_and_read_only():
    manager = Mock()
    live, cache, data, events, clock = setup(manager=manager)
    live.bootstrap()
    reviews = [event for event in events if event.type is EventType.STARTUP_ANALYSIS]
    assert len(reviews) == 4
    assert all(event.get("results")[0]["outcome"] == "BUY" for event in reviews)
    assert reviews[0].bar_time.date() == datetime(2026, 10, 9).date()
    assert render(reviews[0], 3) is None
    assert "09.10.2026" in telegram_render(reviews[0], tz_offset_hours=3)
    assert "Стартовый обзор" in telegram_render(reviews[0], tz_offset_hours=3)
    manager.actions_for_signal.assert_not_called()
    manager.manage.assert_not_called()
    live.step()
    live.step()
    assert len([e for e in events if e.type is EventType.STARTUP_ANALYSIS]) == 4
    manager.actions_for_signal.assert_not_called()
    assert any(e.type is EventType.HEARTBEAT for e in events)


def test_weekend_session_is_chosen_instead_of_friday():
    live, *_ = setup(end="2026-10-11 19:00")
    live.bootstrap()
    assert all(state.received_bar.date() == datetime(2026, 10, 11).date() for state in live.states.values())


def test_three_new_bars_are_analyzed_independently_without_replay_or_duplicates():
    # Робот работает с 06:00: минуты до закрытия бара 10:00 — после запуска.
    live, cache, data, events, clock = setup(now=datetime(2026, 10, 12, 10, 1), end="2026-10-12 06:00",
                                             cutover=datetime(2026, 10, 12, 6, 0))
    live.bootstrap()
    incoming = frame("2026-10-12 09:00")
    # Источник публикует новые данные; GAZP остаётся отстающим.
    data["SBER", "1h"] = incoming
    clock[0] += timedelta(seconds=5)  # следующий шаг ритма опроса
    live.worker.run_until_idle()
    before = len(events)
    live.step()
    decisions = [e for e in events[before:] if e.type is EventType.DECISION and e.instrument == "SBER" and e.timeframe == "1h"]
    # Текущий бар ждёт непрерывную базовую 1m UID и не теряется (задача 6.3).
    assert [e.bar_time.hour for e in decisions] == [8, 9]
    assert "базовая 1m" in live.states["SBER", "1h"].reason
    data["SBER", "1m"] = frame("2026-10-12 09:59", "1m", count=300)
    clock[0] += timedelta(seconds=5)
    live.worker.run_until_idle()
    live.step()
    decisions = [e for e in events[before:] if e.type is EventType.DECISION and e.instrument == "SBER" and e.timeframe == "1h"]
    assert [e.bar_time.hour for e in decisions] == [8, 9, 10]
    assert [e.get("analysis_mode") for e in decisions] == ["historical", "historical", "live"]
    assert not any(e.type is EventType.DECISION and e.instrument == "GAZP" for e in events[before:])
    before = len([e for e in events if e.type is EventType.DECISION])
    live.step()
    assert len([e for e in events if e.type is EventType.DECISION]) == before
    # Старое исправление не генерирует новый сигнал.
    corrected = incoming.iloc[-3:-2].assign(close=110)
    cache.accept_rpc_frame("SBER", "1h", corrected, received_at=clock[0])
    live.step()
    assert len([e for e in events if e.type is EventType.DECISION]) == before


def test_live_decision_is_published_once_right_before_its_admission():
    manager = Mock()
    manager.manage.return_value = ()
    manager.actions_for_signal.return_value = SignalAdmission(
        rejections=(RejectionReason(code="zero-quantity", message="Размер позиции ниже минимального"),))
    live, cache, data, events, clock = setup(now=datetime(2026, 10, 12, 10, 1), end="2026-10-12 06:00",
                                             cutover=datetime(2026, 10, 12, 6, 0), manager=manager)
    live.bootstrap()
    data["SBER", "1h"] = frame("2026-10-12 09:00")
    data["SBER", "1m"] = frame("2026-10-12 09:59", "1m", count=300)
    clock[0] += timedelta(seconds=5)
    live.worker.run_until_idle()
    before = len(events)
    live.step()
    trading = [e for e in events[before:] if e.type in {EventType.DECISION, EventType.REJECTED}
               and e.instrument == "SBER" and e.timeframe == "1h"]
    live_decisions = [e for e in trading if e.type is EventType.DECISION and e.get("analysis_mode") == "live"]
    assert len(live_decisions) == 1
    # Отказ идёт сразу за своим решением и связан с ним ключом, без повторного СИГНАЛ.
    index = trading.index(live_decisions[0])
    assert trading[index + 1].type is EventType.REJECTED
    assert trading[index + 1].get("event_id") == live_decisions[0].get("event_id") != ""


def test_state_messages_continue_without_successful_analysis_and_do_not_spam():
    live, cache, data, events, clock = setup()
    live.bootstrap()
    first = len([e for e in events if e.type is EventType.MARKET_DATA_STATUS])
    live.step()
    assert len([e for e in events if e.type is EventType.MARKET_DATA_STATUS]) == first
    clock[0] += timedelta(hours=1)
    live.step()
    assert len([e for e in events if e.type is EventType.MARKET_DATA_STATUS]) == first*2
    assert any(e.type is EventType.TICK_FINISHED and not e.get("completed") for e in events)


def test_unknown_coverage_in_warmup_is_visible_not_a_fake_hold():
    live, cache, _, events, _ = setup()
    cache.coverage_for = lambda *args: False
    live.bootstrap()
    review = next(e for e in events if e.type is EventType.STARTUP_ANALYSIS)
    assert review.get("results")[0]["outcome"] == ""
    assert "пробел" in review.get("results")[0]["reason"]
    assert not review.get("coverage_complete")
    assert all(state.analyzed_bar is None for state in live.states.values())


def test_closed_session_and_delayed_last_bar_are_separate():
    live, cache, _, events, clock = setup(now=datetime(2026, 10, 9, 21), end="2026-10-09 19:00")
    cal = SessionCalendar("MOEX", ((pd.Timestamp("2026-10-09 07:00"), pd.Timestamp("2026-10-09 20:50")),
        (pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:50"))),
        pd.Timestamp("2026-10-09"), pd.Timestamp("2026-10-13"), pd.Timestamp(clock[0]))
    cache.calendar_for = lambda _: cal
    live.bootstrap()
    status = next(e for e in events if e.type is EventType.MARKET_DATA_STATUS and e.timeframe == "1h")
    assert status.get("session") == "waiting_session"
    assert status.get("data_state") == "waiting_bar"  # deadline финального 20:00–21:00 ещё не истёк
    clock[0] += timedelta(minutes=2)
    live.step()
    assert any(e.type is EventType.MARKET_DATA_STATUS and e.get("data_state") == "delayed" for e in events)


def test_informational_events_show_only_useful_overviews_in_console():
    live, _, _, events, _ = setup()
    live.bootstrap()
    stream = io.StringIO()
    console = ConsoleChannel(stream=stream, tz_offset_hours=3)
    for event in events:
        console.handle(event)
    text = stream.getvalue()
    assert "СТАРТОВЫЙ ОБЗОР" not in text
    assert "09.10.2026" not in text
    assert "Полученная свеча" not in text
    assert "Источник данных" not in text
    assert "ДАННЫЕ АКТУАЛЬНЫ" not in text


def test_incomplete_warmup_expands_finitely_and_reports_available_count(tmp_path):
    now = datetime(2026, 10, 12, 7)
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: now)
    calls = []
    def loader(**kwargs):
        calls.append((kwargs["start_date"], kwargs["end_date"]))
        return frame("2026-10-09 19:00", count=3), "uid"
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: now, 0),
                            market_store=MarketDataStore(tmp_path / "market.sqlite3"))
    instrument = Instrument("SBER", "SBER", "share", "SBER", "uid")
    assert cache.prepare_live_history(instrument, "1h", 100).empty  # торговое чтение без сети
    assert calls == []
    IngestionWorker(cache, settings=LEGACY_QUEUE).run_until_idle()
    result = cache.prepare_live_history(instrument, "1h", 100)
    assert len(result) == 3
    # Начальные 30 дней и одно расширение: 60 дней не дали более ранних свечей,
    # значит достигнут предел доступной истории и 120..960 не запрашиваются.
    lowers = sorted({(pd.Timestamp(now)-start).days for start, _ in calls
                     if (pd.Timestamp(now)-start) % pd.Timedelta(days=1) == pd.Timedelta(0)})
    assert lowers == [30, 60]
    assert len(calls) == len(set(calls))
    status = cache.pair_status(instrument, "1h")
    assert status["history"] == "done" and "3/100" in status["history_error"]


def test_live_warmup_uses_calendar_last_due_as_upper_bound():
    live, cache, _, _, _ = setup()
    due = datetime(2026, 10, 9, 20)
    cache.calendar_expectation = lambda *args, **kwargs: SimpleNamespace(last_due=SimpleNamespace(close=due))
    calls, original = [], cache.prepare_live_history

    def prepare(*args, **kwargs):
        calls.append(kwargs.get("max_close"))
        return original(*args, **kwargs)

    cache.prepare_live_history = prepare
    live.register_needs()

    assert calls and set(calls) == {due}


def test_entry_market_uses_calendar_phase_and_publication_deadline():
    now = datetime(2026, 10, 12, 10, 4)
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: now)
    item = Instrument("SBER", "SBER", "share", "SBER", "uid")
    cache = MarketDataCache(lambda **kwargs: (frame("2026-10-12 10:00", "1m", 1), "uid"), timeline)
    cache.calendar_expectation = lambda *args, **kwargs: SimpleNamespace(
        phase=SimpleNamespace(value="break"), last_due=None,
    )
    assert cache.entry_market(item) == {"valid": False, "reason": "Торговая сессия на перерыве"}

    cache.calendar_expectation = lambda *args, **kwargs: SimpleNamespace(
        phase=SimpleNamespace(value="trading"),
        last_due=SimpleNamespace(close=datetime(2026, 10, 12, 10, 2),
                                 publication_deadline=datetime(2026, 10, 12, 10, 3)),
    )
    assert cache.entry_market(item) == {"valid": False, "reason": "Не получена ожидаемая базовая свеча"}


def test_overview_cache_ignores_hole_before_required_tail_but_not_inside():
    live, cache, _, _, _ = setup()
    live.bootstrap()
    instrument = live.bot._instruments[0]
    df = cache.frame_for(instrument, "1h")
    hole = df["datetime"].iloc[1]
    cache._market_reconciler = SimpleNamespace(interval_state=lambda **kw:
        "unresolved" if kw["open_time"] == hole else "scheduled_closed")
    with_old_hole = pd.concat([df.iloc[:1], df.iloc[2:]], ignore_index=True)
    # Вариант «б»: пробел до запуска без доказательства не блокирует расчёт,
    # но виден в диагностике.
    assert cache.coverage_for(instrument, "1h", with_old_hole)
    assert cache.pair_status(instrument, "1h")["pre_cutover_gaps"] == 1
    # После запуска пробел внутри требуемого хвоста блокирует строго.
    cache._live_cutover = pd.Timestamp("2026-09-01")
    assert not cache.coverage_for(instrument, "1h", with_old_hole)
    assert cache.coverage_for(instrument, "1h", with_old_hole.tail(50))
    hole = df["datetime"].iloc[-3]
    with_recent_hole = df.drop(df.index[-3])
    assert not cache.coverage_for(instrument, "1h", with_recent_hole.tail(50))


def test_end_to_end_session_open_delay_recovery_while_old_gap_stays_empty(tmp_path):
    clock = [datetime(2026, 10, 12, 6, 30)]
    timeline = MultiTimeframeScheduler(["1m", "15m", "1h"], clock=lambda: clock[0])
    budget = RequestBudget(lambda: clock[0], 0)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    rec = MarketDataReconciler(store, source="tbank_exchange", request_budget=budget)
    rec.service_calendar = lambda _: False
    instrument = Instrument("SBER", "SBER", "share", "SBER", "uid")
    cal = SessionCalendar("MOEX", ((pd.Timestamp("2026-10-09 07:00"), pd.Timestamp("2026-10-09 20:00")),
        (pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:00"))),
        pd.Timestamp("2026-10-08"), pd.Timestamp("2026-10-13"), pd.Timestamp(clock[0]))
    rec._ranges["uid"] = [cal]
    data = {"1m": frame("2026-10-09 19:59", "1m"), "15m": frame("2026-10-09 19:45", "15m"),
            "1h": frame("2026-10-09 19:00", "1h")}
    calls = []
    def loader(**kwargs):
        calls.append((kwargs["timeframe"], kwargs["start_date"], kwargs["end_date"]))
        source = data[kwargs["timeframe"]]
        start, end = kwargs["start_date"], kwargs["end_date"]
        keep = pd.Series(True, index=source.index)
        if start is not None:
            keep &= source["datetime"] >= pd.Timestamp(start)
        if end is not None:
            keep &= source["datetime"] < pd.Timestamp(end)
        return source[keep].reset_index(drop=True), "uid"
    cache = MarketDataCache(loader, timeline, request_budget=budget, market_store=store,
                           market_reconciler=rec, recovery_retry_seconds=65)
    worker = IngestionWorker(cache, settings=LEGACY_QUEUE)
    store.set_interval_state(source="tbank_exchange", instrument_uid="uid", interval="1m",
        open_time="2026-09-11 21:00", state="unresolved", evidence="empty_recovery_response")
    events, operations = [], []
    class Channel:
        supported_types = ALL_EVENT_TYPES
        def handle(self, event):
            events.append(event)
    bus = EventBus()
    bus.subscribe(Channel())
    manager = Mock()
    manager.manage.side_effect = lambda *args, **kw: operations.append("manage") or ()
    manager.actions_for_signal.side_effect = lambda *args, **kw: operations.append("admit") or SignalAdmission()
    bot = TradingBot([instrument], bus, {"macd_rsi_stoch": object()}, cache, timeline,
        strategy_factory=lambda *args, **kw: Strategy(), trade_manager=manager,
        share_strategies={"SBER": [Assignment(id=tf, strategy="macd_rsi_stoch", management="levels_rr", filter_profile="raw", timeframe=tf)
                                  for tf in ("15m", "1h")]},
        post_tick=lambda _: operations.append("protect") or True, heartbeat_every_ticks=2,
        live_data_waiter=worker.run_until_idle)
    bot._validate()
    live = LiveAnalysis(bot)
    live.bootstrap()
    assert operations == []
    assert any(e.type is EventType.MARKET_DATA_STATUS and e.get("session") == "waiting_session" for e in events)

    def publish(tf, stamps):
        new = frame(stamps[-1], tf, len(stamps))
        new["datetime"] = pd.to_datetime(stamps)
        data[tf] = pd.concat([data[tf], new], ignore_index=True)
    def poll():
        worker.run_until_idle()
        operations.clear()
        live.step()
        assert operations[0] == "protect"
        if "admit" in operations:
            assert operations.index("protect") < operations.index("admit")

    clock[0] = datetime(2026, 10, 12, 7, 1)
    publish("1m", ["2026-10-12 07:00"])
    poll()
    assert "admit" not in operations
    clock[0] = datetime(2026, 10, 12, 7, 15)
    publish("1m", [f"2026-10-12 07:{i:02d}" for i in range(1, 15)])
    publish("15m", ["2026-10-12 07:00"])
    poll()
    assert "admit" in operations
    assert any(e.type is EventType.DECISION and e.timeframe == "15m" for e in events)
    clock[0] = datetime(2026, 10, 12, 8)
    publish("1m", [f"2026-10-12 07:{i:02d}" for i in range(15, 60)])
    publish("15m", ["2026-10-12 07:15", "2026-10-12 07:30", "2026-10-12 07:45"])
    publish("1h", ["2026-10-12 07:00"])
    poll()
    assert any(e.type is EventType.DECISION and e.timeframe == "1h" and e.bar_time == clock[0] for e in events)
    clock[0] = datetime(2026, 10, 12, 8, 20)
    live.step()
    assert any(e.type is EventType.MARKET_DATA_STATUS and e.timeframe == "15m" and e.get("data_state") == "delayed" for e in events)
    publish("1m", [f"2026-10-12 08:{i:02d}" for i in range(20)])
    publish("15m", ["2026-10-12 08:00"])
    poll()
    statuses = [e for e in events if e.type is EventType.MARKET_DATA_STATUS and e.timeframe == "15m"]
    assert statuses[-1].get("data_state") == "current"
    assert statuses[-1].get("received_bar") == datetime(2026, 10, 12, 8, 15)
    # Старую несвязанную дыру пытались восстановить отдельным бюджетом.
    assert any(start == pd.Timestamp("2026-09-11 21:00") for _, start, _ in calls)
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m",
        open_time="2026-09-11 21:00") == "unresolved"
    assert store.cursors(source="tbank_exchange", instrument_uid="uid", interval="1m")["processed_through"] is None


def test_old_market_store_migration_preserves_candles_coverage_cursors_and_analysis(tmp_path):
    import sqlite3
    path = tmp_path / "market.sqlite3"
    store = MarketDataStore(path)
    source, uid, interval = "tbank_exchange", "uid", "1m"
    store.record_candles(source=source, instrument_uid=uid, interval=interval,
        candles=frame("2026-10-09 19:59", interval, 3))
    store.mark_processed(source=source, instrument_uid=uid, interval=interval, open_time="2026-10-09 19:59")
    before = store.source_info()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE recovery_attempts")
        connection.execute("DROP TABLE analysis_cursors")
    reopened = MarketDataStore(path)
    assert reopened.source_info() == before
    assert len(reopened.current_frame(source=source, instrument_uid=uid, interval=interval)) == 3
    assert reopened.cursors(source=source, instrument_uid=uid, interval=interval)["processed_through"] == "2026-10-09T19:59:00Z"
    reopened.mark_analyzed(source=source, instrument_uid=uid, interval="1h", assignment_id="strategy", open_time="2026-10-09 19:00")
    again = MarketDataStore(path)
    assert again.analysis_cursor(source=source, instrument_uid=uid, interval="1h", assignment_id="strategy") == pd.Timestamp("2026-10-09 19:00")
