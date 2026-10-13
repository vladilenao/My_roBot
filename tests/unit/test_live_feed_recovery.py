from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
import threading

import pandas as pd
import pytest
from t_tech.invest.schemas import TradeSourceType

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler, SessionCalendar, _calendar_from_response
from src.data.request_budget import RequestBudget, RequestDeferred
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


def candles(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps), "open": [100]*len(stamps),
                         "high": [101]*len(stamps), "low": [99]*len(stamps),
                         "close": [100]*len(stamps), "volume": [1]*len(stamps), "is_complete": [True]*len(stamps)})


def test_calendar_sdk_intervals_and_unknown_dates():
    from t_tech.invest.schemas import TradingInterval, TradingDay, TimeInterval, TradingSchedule, TradingSchedulesResponse
    day = datetime(2026, 10, 12)
    response = TradingSchedulesResponse(exchanges=[TradingSchedule(exchange="MOEX", days=[
        TradingDay(date=day, is_trading_day=True, intervals=[TradingInterval(type="main",
            interval=TimeInterval(start_ts=day + timedelta(hours=7), end_ts=day + timedelta(hours=20)))])])])
    calendar = _calendar_from_response(response, "MOEX", observed_at=day)
    assert calendar.covers(day)
    assert not calendar.is_open(day)
    assert calendar.is_open(day + timedelta(hours=9))
    assert not calendar.covers(datetime(2026, 9, 12))


def test_calendar_evidence_reclassifies_persisted_gap_and_exports(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    now = pd.Timestamp("2026-10-12 09:00")
    budget = RequestBudget(lambda: now, 0)
    rec = MarketDataReconciler(store, source="tbank_exchange", request_budget=budget)
    gap = pd.Timestamp("2026-10-12 06:00")
    store.set_interval_state(source="tbank_exchange", instrument_uid="uid", interval="1m",
                             open_time=gap, state="unresolved", evidence="schedule_unavailable")
    rec._ranges["uid"] = [SessionCalendar("MOEX", ((pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:00")),),
        gap.normalize(), gap.normalize()+pd.Timedelta(days=1), now)]
    rec.recheck("uid", "1m", now)
    assert store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time=gap) == "scheduled_closed"
    changes = store.changes_after(store.producer_id, 0)["changes"]
    assert changes[-1]["interval_status"]["evidence"] == "applicable_trading_schedule"
    assert store.cursors(source="tbank_exchange", instrument_uid="uid", interval="1m")["processed_through"] is None


def test_recovery_cooldown_is_additive_and_survives_reopen(tmp_path):
    path = tmp_path / "market.sqlite3"
    now = datetime(2026, 10, 12, 7)
    store = MarketDataStore(path)
    store.set_interval_state(source="s", instrument_uid="u", interval="1m", open_time=now,
                             state="unresolved", evidence="empty_recovery_response")
    store.record_recovery_attempt(source="s", instrument_uid="u", interval="1m", open_time=now,
                                   now=now, next_attempt=now+timedelta(seconds=60))
    reopened = MarketDataStore(path)
    assert reopened.first_recovery_candidate(source="s", instrument_uid="u", interval="1m", now=now) is None
    assert reopened.first_recovery_candidate(source="s", instrument_uid="u", interval="1m", now=now+timedelta(seconds=60)) == now


def test_budget_shared_rate_limit_and_inflight_dedup():
    clock = [datetime(2026, 10, 12, 7)]
    budget = RequestBudget(lambda: clock[0], 5)
    with pytest.raises(RuntimeError):
        budget.call(lambda: (_ for _ in ()).throw(RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=30")), key="fresh")
    clock[0] += timedelta(seconds=29)
    with pytest.raises(RequestDeferred):
        budget.call(lambda: None, key="recovery")
    clock[0] += timedelta(seconds=1)
    assert budget.call(lambda: "ok", key="recovery") == "ok"


def test_real_loader_paces_load_once_and_bursts_pages(monkeypatch):
    from src.data.loader import load_candles
    from t_tech.invest.schemas import HistoricCandle, Quotation
    from datetime import timezone
    clock = [datetime(2026, 10, 12, 8)]
    requests = []
    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
    budget = RequestBudget(lambda: clock[0], 5, sleep=sleep)
    lower, middle, upper = (datetime(2026, 10, day, tzinfo=timezone.utc) for day in (10, 11, 12))
    monkeypatch.setattr("t_tech.invest.utils.get_intervals", lambda *args: [(lower, middle), (middle, upper)])
    def get_candles(**kwargs):
        requests.append(clock[0])
        return SimpleNamespace(candles=[HistoricCandle(time=kwargs["from_"],
            open=Quotation(units=100, nano=0), high=Quotation(units=101, nano=0), low=Quotation(units=99, nano=0),
            close=Quotation(units=100, nano=0), volume=1, is_complete=True)])
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(market_data=SimpleNamespace(get_candles=get_candles))
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0])
    def load():
        return load_candles("SBER", "share", "1m", instrument_id="uid", client_provider=Provider(),
            start_date=lower, end_date=upper, request_budget=budget, clock=timeline.clock,
            wait_for_budget=True, single_attempt=True)
    data, uid = load()
    load()
    assert uid == "uid" and len(data) == 2
    # Страницы одной загрузки идут подряд, а не через паузу refresh-интервала.
    assert requests[1] == requests[0]
    assert requests[3] == requests[2]
    # Новая загрузка берёт отдельное окно: её первая страница отстоит на интервал.
    assert requests[2] - requests[0] == timedelta(seconds=5)


def test_cold_start_warmup_reads_local_store_without_candle_stream(tmp_path):
    now = datetime(2026, 10, 12, 8, 30)
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: now)
    budget = RequestBudget(lambda: now, 5)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source="tbank_exchange", instrument_uid="uid", interval="1h",
                         candles=candles("2026-10-12 07:00"))
    catalogs, streams = [], []

    def shares(**kwargs):
        catalogs.append(kwargs)
        return SimpleNamespace(instruments=[SimpleNamespace(ticker="SBER", uid="uid")])

    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(instruments=SimpleNamespace(shares=shares))

    def loader(**kwargs):
        streams.append(kwargs["timeframe"])
        return candles("2026-10-12 07:00"), "uid"

    cache = MarketDataCache(loader, timeline, request_budget=budget, market_store=store,
                            market_source="tbank_exchange", client_provider=Provider())
    instrument = Instrument("SBER", "SBER", "share")
    assert cache.prepare_live_history(instrument, "1h", 1).empty  # торговое чтение без сети
    IngestionWorker(cache).run_until_idle()
    result = cache.prepare_live_history(instrument, "1h", 1)
    assert len(result) == 1
    assert catalogs  # UID узнан дешёвым запросом каталога
    assert streams == []  # свечи при прогреве из базы из API не тянулись


def test_same_inflight_rpc_is_not_sent_twice():
    now = datetime(2026, 10, 12, 8)
    budget = RequestBudget(lambda: now, 0)
    entered, finish = threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert finish.wait(2)
    thread = threading.Thread(target=lambda: budget.call(slow, key="same-range"))
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RequestDeferred):
            budget.call(lambda: pytest.fail("Дубликат RPC"), key="same-range")
    finally:
        finish.set()
        thread.join(2)


def _registered(cache, store, item, timeframe, stamps):
    store.record_candles(source="tbank_exchange", instrument_uid=item.uid, interval=timeframe, candles=candles(*stamps))
    cache.register_live_pair(item, item.uid, timeframe)
    cache.mark_live_history(item.uid, timeframe, done=True)


def test_bounded_window_in_middle_of_hour_includes_last_closed_hour(tmp_path):
    now = datetime(2026, 10, 12, 9, 30)
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: now)
    calls = []
    def loader(**kwargs):
        calls.append(kwargs["start_date"])
        data = candles("2026-10-12 08:00")
        return data[data["datetime"] >= kwargs["start_date"]], "uid"
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: now, 0),
                           data_backfill_window_seconds=3600, market_store=store)
    item = Instrument("SBER", "SBER", "share", "SBER", "uid")
    _registered(cache, store, item, "1h", ["2026-10-12 07:00"])
    IngestionWorker(cache).run_until_idle()
    assert calls == [pd.Timestamp("2026-10-12 08:00")]
    assert cache.frame_for(item, "1h")["datetime"].max() == pd.Timestamp("2026-10-12 08:00")


def test_closed_session_finalization_requests_only_that_bar(tmp_path):
    now = datetime(2026, 10, 13, 6)
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: now)
    cal = SessionCalendar("MOEX", ((pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:50")),),
        pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-14"), pd.Timestamp(now))
    calls = []
    def loader(**kwargs):
        calls.append((kwargs["start_date"], kwargs["end_date"]))
        return candles("2026-10-12 20:00"), "uid"
    store = MarketDataStore(tmp_path / "market.sqlite3")
    budget = RequestBudget(lambda: now, 0)
    rec = MarketDataReconciler(store, source="tbank_exchange", request_budget=budget)
    rec._ranges["uid"] = [cal]
    rec.service_calendar = lambda _: False
    cache = MarketDataCache(loader, timeline, request_budget=budget, market_store=store,
        market_reconciler=rec, data_backfill_window_seconds=3600)
    item = Instrument("SBER", "SBER", "share", "SBER", "uid")
    _registered(cache, store, item, "1h", ["2026-10-12 19:00"])
    IngestionWorker(cache).run_until_idle()
    assert calls == [(pd.Timestamp("2026-10-12 20:00"), pd.Timestamp("2026-10-12 21:00"))]


def test_calendar_rpc_queue_is_deferred_and_reused_from_durable_store(tmp_path):
    clock = [pd.Timestamp("2026-10-12 09:00")]
    budget = RequestBudget(lambda: clock[0], 5)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    calls = []
    def rpc(name, value):
        def execute(**kwargs):
            calls.append(name)
            return value
        return execute
    day = SimpleNamespace(date=clock[0].normalize(), is_trading_day=True, intervals=[],
        start_time=clock[0].normalize()+pd.Timedelta(hours=7), end_time=clock[0].normalize()+pd.Timedelta(hours=20))
    client = SimpleNamespace(instruments=SimpleNamespace(
        get_instrument_by=rpc("instrument", SimpleNamespace(instrument=SimpleNamespace(exchange="MOEX"))),
        trading_schedules=rpc("schedule", SimpleNamespace(exchanges=[SimpleNamespace(exchange="MOEX", days=[day])]))),
        market_data=SimpleNamespace(get_trading_status=rpc("status", SimpleNamespace(__annotations__={}))))
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield client
    rec = MarketDataReconciler(store, source="s", request_budget=budget, client_provider=Provider())
    assert rec.session_calendar("uid", clock[0]) is None
    assert rec.service_calendar(clock[0])
    assert not rec.service_calendar(clock[0])
    clock[0] += pd.Timedelta(seconds=5)
    assert rec.service_calendar(clock[0])
    clock[0] += pd.Timedelta(seconds=5)
    assert rec.service_calendar(clock[0])
    assert calls == ["instrument", "status", "schedule"]
    reopened = MarketDataReconciler(MarketDataStore(store.path), source="s", request_budget=budget)
    assert reopened.session_calendar("uid", clock[0]).is_open(clock[0])
    assert not reopened._calendar_tasks


@pytest.mark.parametrize("point,expected", [
    ("2026-10-09 23:00", "scheduled_closed"),
    ("2026-10-10 12:00", "unresolved"),  # выходная сессия действительно открыта
    ("2026-10-11 12:00", "scheduled_closed"),
    ("2026-09-11 21:00", "unresolved"),
])
def test_session_breaks_and_weekend_trading_are_not_guessed(tmp_path, point, expected):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    now = pd.Timestamp("2026-10-12 09:00")
    rec = MarketDataReconciler(store, source="s", request_budget=RequestBudget(lambda: now, 0))
    rec._ranges["uid"] = [SessionCalendar("MOEX", ((pd.Timestamp("2026-10-10 10:00"), pd.Timestamp("2026-10-10 19:00")),),
        pd.Timestamp("2026-10-09"), pd.Timestamp("2026-10-13"), now)]
    assert rec.interval_state(instrument_uid="uid", interval="1m", open_time=point, now=now) == expected


def test_permanent_old_gap_does_not_starve_fresh_pairs_and_cache_read(tmp_path):
    clock = [datetime(2026, 10, 12, 8, 5)]
    timeline = MultiTimeframeScheduler(["1m", "1h"], clock=lambda: clock[0])
    budget = RequestBudget(lambda: clock[0], 0)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    rec = MarketDataReconciler(store, source="tbank_exchange", request_budget=budget)
    # Изолируем сетевой календарь: покрытие текущего дня известно.
    rec.service_calendar = lambda _: False
    cal = SessionCalendar("MOEX", ((pd.Timestamp("2026-10-09 07:00"), pd.Timestamp("2026-10-09 20:00")),
                                   (pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:00"))),
                          pd.Timestamp("2026-10-09"), pd.Timestamp("2026-10-13"), pd.Timestamp(clock[0]))
    calls = []
    entered, release = threading.Event(), threading.Event()
    def loader(**kwargs):
        calls.append((kwargs["ticker"], kwargs["timeframe"], kwargs["start_date"], kwargs["end_date"]))
        if pd.Timestamp(kwargs["start_date"]) < pd.Timestamp("2026-09-30"):
            return pd.DataFrame(), kwargs["instrument_id"]  # старая дыра всегда пуста
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return candles("2026-10-12 07:00" if kwargs["timeframe"] == "1h" else "2026-10-12 08:04"), kwargs["instrument_id"]
    cache = MarketDataCache(loader, timeline, request_budget=budget, market_store=store, market_reconciler=rec)
    instruments = [Instrument(name, name, "share", name, "uid-"+name) for name in ("A", "B", "C")]
    for item in instruments:
        rec._ranges[item.uid] = [cal]
        for tf in ("1m", "1h"):
            _registered(cache, store, item, tf, ["2026-10-09 19:00"])
        store.set_interval_state(source="tbank_exchange", instrument_uid=item.uid, interval="1m",
            open_time="2026-09-11 21:00", state="unresolved", evidence="empty_recovery_response")
    worker = IngestionWorker(cache)
    lane = threading.Thread(target=worker.step)
    lane.start()
    assert entered.wait(1)
    assert not cache.frame_for(instruments[0], "1m").empty  # сеть не держит lock кэша
    release.set()
    lane.join(3)
    for _ in range(40):
        worker.step()
        clock[0] += timedelta(seconds=1)
    fresh = {(name, tf) for name, tf, _, end in calls if end is None}  # свежее окно без верхней границы
    assert {name for name, _ in fresh} == {"A", "B", "C"}
    assert {tf for _, tf in fresh} == {"1m", "1h"}
    assert any(pd.Timestamp(start) == pd.Timestamp("2026-09-11 21:00") for _, _, start, _ in calls)
    for item in instruments:
        assert cache.frame_for(item, "1m")["datetime"].max() == pd.Timestamp("2026-10-12 08:04")
        assert store.coverage_state(source="tbank_exchange", instrument_uid=item.uid, interval="1m",
                                    open_time="2026-09-11 21:00") == "unresolved"
        assert store.cursors(source="tbank_exchange", instrument_uid=item.uid, interval="1m")["processed_through"] is None


def test_schedule_is_requested_from_now_and_past_dates_are_not_requested(tmp_path):
    # Брокер отклоняет `from` раньше текущей даты (INVALID_ARGUMENT 30003, проба 8.5).
    now = pd.Timestamp("2026-10-12 20:30")
    budget = RequestBudget(lambda: now, 0)
    requests = []
    day = SimpleNamespace(date=now.normalize(), is_trading_day=True, intervals=[],
                          start_time=now.normalize() + pd.Timedelta(hours=7), end_time=now.normalize() + pd.Timedelta(hours=21))
    def schedules(**kwargs):
        if pd.Timestamp(kwargs["from_"]).tz_convert(None) < now:
            raise AssertionError("`from` can't be less than the current date")
        requests.append(kwargs)
        return SimpleNamespace(exchanges=[SimpleNamespace(exchange="MOEX", days=[day])])
    client = SimpleNamespace(instruments=SimpleNamespace(
        get_instrument_by=lambda **_: SimpleNamespace(instrument=SimpleNamespace(exchange="MOEX")),
        trading_schedules=schedules),
        market_data=SimpleNamespace(get_trading_status=lambda **_: SimpleNamespace(__annotations__={})))
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield client
    rec = MarketDataReconciler(MarketDataStore(tmp_path / "market.sqlite3"), source="s",
                               request_budget=budget, client_provider=Provider())
    assert rec.interval_state(instrument_uid="uid", interval="1m", open_time="2026-10-09 23:00", now=now) == "unresolved"
    assert not rec._calendar_tasks  # за прошлую дату RPC не планируется
    rec.session_calendar("uid", now)
    while rec.service_calendar(now):
        pass
    assert requests and all(pd.Timestamp(item["from_"]).tz_convert(None) >= now for item in requests)
    assert rec.session_calendar("uid", now).is_open(now)


def _broker_day(day, intervals):
    """Форма TradingDay из ответа брокера (проба 8.5, 13.10.2026)."""
    utc = lambda value: pd.Timestamp(f"{day} {value}", tz="UTC").to_pydatetime()
    return SimpleNamespace(date=pd.Timestamp(day, tz="UTC").to_pydatetime(), is_trading_day=True,
        intervals=[SimpleNamespace(type=kind, interval=SimpleNamespace(start_ts=utc(a), end_ts=utc(b)))
                   for kind, a, b in intervals])


def test_broker_schedule_auctions_and_clearing_are_breaks_and_exchange_case_is_ignored():
    shares = _broker_day("2026-10-13", [
        ("opening_auction_morning", "03:50:00", "03:59:59"), ("opening_auction", "03:50:00", "03:59:59"),
        ("regular_trading_session_morning", "04:00:00", "05:59:59"), ("regular_trading_session", "04:00:00", "05:59:59"),
        ("regular_trading_session_main", "06:00:00", "15:54:59"), ("regular_trading_session", "06:00:00", "15:54:59"),
        ("closing_auction_main", "15:55:00", "15:59:30"), ("closing_auction", "15:55:00", "15:59:30"),
        ("regular_trading_session_evening", "16:00:01", "20:49:59"), ("regular_trading_session", "16:00:01", "20:49:59")])
    response = SimpleNamespace(exchanges=[SimpleNamespace(exchange="MOEX_MRNG_EVNG_E_WKND_DLR", days=[shares])])
    cal = _calendar_from_response(response, "moex_mrng_evng_e_wknd_dlr")
    assert cal is not None and len(cal.intervals) == 3  # дубли схлопнуты
    at = lambda value: pd.Timestamp(f"2026-10-13 {value}")
    assert not cal.is_open(at("03:55"))  # аукцион открытия
    assert cal.is_open(at("05:59")) and cal.is_open(at("06:00")) and cal.is_open(at("15:54"))
    assert not cal.is_open(at("15:57"))  # аукцион закрытия
    assert cal.is_open(at("16:00")) and cal.is_open(at("20:49")) and not cal.is_open(at("20:50"))
    futures = _broker_day("2026-10-13", [
        ("regular_trading_session_evening", "16:00:00", "20:50:00"), ("clearing", "20:50:01", "21:30:00"),
        ("weekend_session_new_kind", "21:30:00", "22:00:00")])
    cal = _calendar_from_response(SimpleNamespace(exchanges=[SimpleNamespace(exchange="FORTS_FUTURES_WEEKEND", days=[futures])]),
                                  "forts_futures_weekend")
    assert cal.is_open(at("20:49")) and not cal.is_open(at("20:50"))  # конец ровно на минуте её не включает
    assert not cal.is_open(at("21:00"))  # вечерний клиринг — перерыв
    assert cal.is_open(at("21:45"))  # неизвестный тип — консервативно торги


def _no_trade_setup(tmp_path, now, stored, exchange, trades):
    """Неликвидная пара: свеча есть только в минуте со сделкой (проба 8.5, NGZ6)."""
    # Робот запущен до пустых минут: граница запуска (вариант «б») их не скрывает.
    target, clock = pd.Timestamp(now), [pd.Timestamp(stored[-1]) + pd.Timedelta(seconds=70)]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    budget = RequestBudget(lambda: clock[0], 0)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    rec = MarketDataReconciler(store, source="tbank_exchange", request_budget=budget)
    rec.service_calendar = lambda _: False
    rec._ranges["uid"] = [SessionCalendar("MOEX", ((pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 20:00")),),
        pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-13"), clock[0])]
    def loader(**kwargs):
        frame = candles(*exchange)
        frame = frame[frame["datetime"] >= kwargs["start_date"]]
        if kwargs.get("end_date") is not None:
            frame = frame[frame["datetime"] < kwargs["end_date"]]
        return frame, "uid"
    requests = []
    def get_last_trades(*, instrument_id, from_, to, trade_source):
        assert trade_source == TradeSourceType.TRADE_SOURCE_ALL  # дилерские сделки тоже
        requests.append((pd.Timestamp(from_).tz_convert(None), pd.Timestamp(to).tz_convert(None)))
        stamps = [pd.Timestamp(item) for item in trades]
        return SimpleNamespace(trades=[SimpleNamespace(time=stamp.tz_localize("UTC").to_pydatetime())
                                       for stamp in stamps if requests[-1][0] <= stamp < requests[-1][1]])
    client = SimpleNamespace(market_data=SimpleNamespace(get_last_trades=get_last_trades))
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield client
    cache = MarketDataCache(loader, timeline, request_budget=budget, market_store=store,
                            market_reconciler=rec, client_provider=Provider())
    item = Instrument("NGZ6", "NGZ6", "future", "NGZ6", "uid")
    _registered(cache, store, item, "1m", stored)
    worker = IngestionWorker(cache)
    worker.run_until_idle()
    clock[0] = target
    return cache, store, worker, clock, requests


def _states(store, start, end):
    return {stamp: store.coverage_state(source="tbank_exchange", instrument_uid="uid", interval="1m", open_time=stamp)
            for stamp in pd.date_range(start, end, freq="1min")}


def test_minutes_without_trades_are_confirmed_and_traded_minute_without_candle_stays_gap(tmp_path):
    cache, store, worker, clock, requests = _no_trade_setup(tmp_path, "2026-10-12 12:10:05",
        stored=["2026-10-12 12:00"], exchange=["2026-10-12 12:00", "2026-10-12 12:06"],
        trades=["2026-10-12 12:04:20", "2026-10-12 12:06:01"])  # в 12:04 сделка есть, свечи нет
    for _ in range(5):
        worker.step()
    states = _states(store, "2026-10-12 12:01", "2026-10-12 12:09")
    assert requests and all(end <= pd.Timestamp("2026-10-12 12:10") for _, end in requests)
    assert [stamp.minute for stamp, state in states.items() if state == "confirmed_no_trade"] == [1, 2, 3, 5, 7, 8]
    assert states[pd.Timestamp("2026-10-12 12:09")] is None  # срок публикации ещё не истёк
    assert states[pd.Timestamp("2026-10-12 12:04")] not in {"confirmed_no_trade", "received"}
    assert states[pd.Timestamp("2026-10-12 12:06")] == "received"
    resolved = store.cursors(source="tbank_exchange", instrument_uid="uid", interval="1m")["resolved_through"]
    assert pd.Timestamp(resolved).tz_convert(None) == pd.Timestamp("2026-10-12 12:03")  # префикс до настоящей дыры


def test_no_trade_evidence_covers_only_last_hour_and_rejects_truncated_answer(tmp_path):
    cache, store, worker, clock, requests = _no_trade_setup(tmp_path, "2026-10-12 12:10:05",
        stored=["2026-10-12 10:00"], exchange=["2026-10-12 10:00"], trades=[])
    for _ in range(5):
        worker.step()
    states = _states(store, "2026-10-12 10:01", "2026-10-12 12:09")
    assert all(start >= pd.Timestamp("2026-10-12 11:15") for start, _ in requests)
    assert not any(state == "confirmed_no_trade" for stamp, state in states.items() if stamp < pd.Timestamp("2026-10-12 11:15"))
    assert {state for stamp, state in states.items()
            if pd.Timestamp("2026-10-12 11:16") <= stamp <= pd.Timestamp("2026-10-12 12:08")} == {"confirmed_no_trade"}

    busy = [f"2026-10-12 12:05:{second:02d}.{part:03d}" for second in range(50) for part in range(20)]
    _, store, worker, _, requests = _no_trade_setup(tmp_path / "busy", "2026-10-12 12:10:05",
        stored=["2026-10-12 12:00"], exchange=["2026-10-12 12:00"], trades=busy)
    for _ in range(5):
        worker.step()
    assert requests
    assert "confirmed_no_trade" not in _states(store, "2026-10-12 12:01", "2026-10-12 12:09").values()
