"""Фоновая unary-полоса: последовательность, полный recovery, раунды и бюджет."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker, RecoveryRange, chunk_windows
from src.data.ingestion_settings import MarketDataSettings
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler, SessionCalendar
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import CandleScheduler, MultiTimeframeScheduler
from src.assembly import contour, core

# Прежняя общая очередь — откат при independent_lanes = false.
LEGACY_QUEUE = MarketDataSettings(independent_lanes=False)

SOURCE, UID = "tbank_exchange", "u"
ITEM = Instrument("SBER", "SBER", "share", "SBER", UID)


def bars(start, count, *, minutes=1, price=100):
    stamps = pd.date_range(pd.Timestamp(start), periods=count, freq=f"{minutes}min")
    return pd.DataFrame({"datetime": stamps, "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1, "is_complete": True})


class Exchange:
    """Источник GetCandles: окно [start, end) либо включённая верхняя граница."""

    def __init__(self, frame, *, inclusive_upper=False):
        self.frame = frame
        self.inclusive_upper = inclusive_upper
        self.calls = []
        self.fail = set()

    def loader(self, **kwargs):
        budget = kwargs.get("request_budget")
        def serve():
            return self._serve(kwargs)
        if budget is None:
            return serve()
        # Как реальный загрузчик: каждая страница идёт через общий бюджет.
        return budget.call(serve, key=(kwargs["timeframe"], kwargs["start_date"], kwargs["end_date"]),
                           wait=kwargs.get("wait_for_budget", True), pace=kwargs.get("pace", True))

    def _serve(self, kwargs):
        start, end = kwargs["start_date"], kwargs["end_date"]
        self.calls.append((kwargs["timeframe"], start, end))
        if (start, end) in self.fail:
            raise RuntimeError("UNAVAILABLE")
        stamps = self.frame["datetime"]
        keep = pd.Series(True, index=self.frame.index)
        if start is not None:
            keep &= stamps >= pd.Timestamp(start)
        if end is not None:
            keep &= (stamps <= pd.Timestamp(end)) if self.inclusive_upper else (stamps < pd.Timestamp(end))
        return self.frame[keep].reset_index(drop=True), UID


def setup(tmp_path, exchange, now, *, settings=None, stored=None, calendar=None, per_minute=None, interval=0):
    clock = [pd.Timestamp(now)]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    budget = RequestBudget(lambda: clock[0], interval, per_minute=per_minute, sleep=lambda _: None)
    reconciler = None
    if calendar is not None:
        reconciler = MarketDataReconciler(store, source=SOURCE, request_budget=budget)
        reconciler._ranges[UID] = [calendar]
        reconciler.service_calendar = lambda _: False
    # Робот работает с начала сохранённых данных: пропуски после этого
    # момента проверяются строго (вариант «б» не освобождает их).
    cutover = None if stored is None else stored["datetime"].min()
    cache = MarketDataCache(exchange.loader, timeline, request_budget=budget, market_store=store,
                            market_reconciler=reconciler, data_refresh_min_interval=interval, live_cutover=cutover)
    if stored is not None:
        store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=stored)
    cache.register_live_pair(ITEM, UID, "1m")
    cache.mark_live_history(UID, "1m", done=True)
    worker = IngestionWorker(cache, settings=settings or MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30))
    return cache, store, worker, clock


def received(cache):
    return list(cache.frame_for(ITEM, "1m")["datetime"])


def book(store):
    return RecoveryRange.from_json(store.live_state(kind="recovery", source=SOURCE, instrument_uid=UID, interval="1m"))


def test_chunks_overlap_by_native_period_and_respect_span():
    grid = CandleScheduler("1m")
    windows = chunk_windows("1m", datetime(2026, 9, 12), datetime(2026, 10, 12), grid)
    assert all(upper - lower <= timedelta(days=1) for lower, upper in windows)
    assert all(nxt[0] == upper - timedelta(minutes=1) for (_, upper), nxt in zip(windows, windows[1:]))
    assert windows[0][0] == datetime(2026, 9, 12) and windows[-1][1] == datetime(2026, 10, 12)
    hourly = chunk_windows("1h", datetime(2026, 9, 1), datetime(2026, 10, 1), CandleScheduler("1h"))
    assert all(upper - lower <= timedelta(days=7) for lower, upper in hourly)
    with pytest.raises(ValueError):
        chunk_windows("7m", datetime(2026, 9, 1), datetime(2026, 9, 2), grid)


def test_trailing_gap_waits_for_publication_deadline_then_recovers(tmp_path):
    exchange = Exchange(bars("2026-10-12 12:00", 3))
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 12:01:10",
                                        stored=bars("2026-10-12 12:00", 1))
    # Нет ни одного события после 12:00; 12:01 ещё в пределах срока публикации.
    clock[0] = pd.Timestamp("2026-10-12 12:02:10")
    worker.detect()
    assert book(store) is None
    clock[0] = pd.Timestamp("2026-10-12 12:03:00")
    worker.detect()
    assert book(store).lower == pd.Timestamp("2026-10-12 12:01")
    worker.run_until_idle()
    assert received(cache)[-1] == pd.Timestamp("2026-10-12 12:02")
    assert book(store) is None


@pytest.mark.parametrize("outage", [timedelta(minutes=5), timedelta(hours=3)])
def test_full_outage_is_recovered_without_one_hour_cutoff(tmp_path, outage):
    start = pd.Timestamp("2026-10-12 09:00")
    end = start + outage
    exchange = Exchange(bars(start, int(outage / timedelta(minutes=1)) + 1))
    cache, store, worker, clock = setup(tmp_path, exchange, end + timedelta(minutes=1, seconds=40),
                                        stored=bars(start, 1))
    # Соединение вернулось: последняя минута уже получена через новый high-water.
    cache.accept_rpc_frame(UID, "1m", bars(end, 1), received_at=clock[0])
    worker.run_until_idle()
    assert received(cache) == list(pd.date_range(start, end, freq="1min"))
    resolved = store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")["resolved_through"]
    assert pd.Timestamp(resolved).tz_localize(None) == end
    recovery = [call for call in exchange.calls if call[2] is not None]
    assert min(call[1] for call in recovery) == start + timedelta(minutes=1)
    assert book(store) is None


@pytest.mark.parametrize("inclusive", [True, False])
def test_chunk_edge_gives_one_canonical_candle_for_any_upper_inclusion(tmp_path, inclusive):
    start = pd.Timestamp("2026-10-10 09:00")
    exchange = Exchange(bars(start, 2 * 24 * 60), inclusive_upper=inclusive)
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 09:01:00", stored=bars(start, 1))
    worker.run_until_idle()
    frame = cache.frame_for(ITEM, "1m")
    assert frame["datetime"].is_unique
    assert len(frame) == 2 * 24 * 60
    total, after = 0, 0
    while True:
        page = store.changes_after(store.producer_id, after)
        total += len(page["changes"])
        if not page["has_more"]:
            break
        after = page["next_after_id"]
    assert total == 2 * 24 * 60  # дубль на стыке чанков не создал вторую версию


def test_each_page_uses_minute_budget_and_successful_chunks_survive_failure(tmp_path):
    start = pd.Timestamp("2026-10-09 09:00")
    exchange = Exchange(bars(start, 3 * 24 * 60))
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 09:01:00",
                                        stored=bars(start, 1), per_minute=2)
    worker.detect()
    third = chunk_windows("1m", book(store).checkpoint, pd.Timestamp("2026-10-12 09:00"), CandleScheduler("1m"))[2]
    exchange.fail.add(third)
    while worker.step():
        pass
    # Потолок 2 RPC в минуту: страница сверх него откладывается, не отправляется.
    assert len(exchange.calls) == 2
    clock[0] += timedelta(minutes=1)
    while worker.step():
        pass
    saved = book(store)
    assert saved.checkpoint == third[0]  # два чанка сохранены до сбоя третьего
    assert saved.attempts == 1
    assert received(cache)[-1] >= third[0]


def test_empty_responses_park_range_without_fast_cycle_and_survive_restart(tmp_path):
    exchange = Exchange(bars("2026-10-12 12:00", 1))
    settings = MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30, recovery_max_attempts=3,
                                  recovery_recheck_interval_secs=600)
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 12:10:00", settings=settings,
                                        stored=bars("2026-10-12 12:00", 1))
    for _ in range(20):
        worker.run_until_idle()
        clock[0] += timedelta(seconds=40)
    saved = book(store)
    assert saved.state == "parked" and saved.attempts == 3
    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m",
                                open_time=saved.checkpoint) == "unresolved"
    assert cache.pair_status(ITEM, "1m")["state"] == "GAP_HOLD"
    # Окна recovery начинаются с пропуска; плановая сверка последних баров —
    # отдельный недорогой ритм и интенсивным раундом не является.
    def recovery():
        return [call for call in exchange.calls
                            if call[2] is not None and call[1] >= pd.Timestamp("2026-10-12 12:01")]
    calls = len(recovery())
    for _ in range(5):
        clock[0] += timedelta(seconds=40)
        worker.run_until_idle()
    # parked: свежий опрос продолжается, но нового интенсивного раунда нет.
    assert len(recovery()) == calls
    assert book(store).state == "parked"
    # Restart: состояние раунда берётся из store, а не сбрасывается.
    cache2, _, worker2, clock2 = setup(tmp_path, exchange, clock[0], settings=settings)
    parked = book(store)
    assert parked.attempts == 3 and parked.state == "parked"
    clock2[0] = parked.next_retry_at + timedelta(seconds=1)
    exchange.frame = bars("2026-10-12 12:00", 60)  # источник снова отдаёт весь период
    assert worker2.step()  # одна bounded parked-проверка
    worker2.run_until_idle()
    assert book(store) is None  # позднее реальное восстановление снимает область


def test_round_time_limit_parks_before_attempt_limit(tmp_path):
    exchange = Exchange(bars("2026-10-12 12:00", 1))
    settings = MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30, recovery_max_attempts=50,
                                  recovery_round_timeout_secs=120, recovery_backoff_max_secs=10)
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 12:05:00", settings=settings,
                                        stored=bars("2026-10-12 12:00", 1))
    for _ in range(30):
        worker.run_until_idle()
        clock[0] += timedelta(seconds=11)
    saved = book(store)
    assert saved.state == "parked"
    assert saved.attempts < 50
    # Новые свечи не продлевают раунд: срок отсчитан от начала раунда.
    assert (saved.next_retry_at - pd.Timestamp("2026-10-12 12:05:00")).total_seconds() >= 120


def test_calendar_break_needs_no_candle_but_unknown_schedule_is_recovered(tmp_path):
    calendar = SessionCalendar("MOEX", ((pd.Timestamp("2026-10-12 07:00"), pd.Timestamp("2026-10-12 12:00")),
                                        (pd.Timestamp("2026-10-12 13:00"), pd.Timestamp("2026-10-12 20:00"))),
                               pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-13"), pd.Timestamp("2026-10-12 12:30"))
    exchange = Exchange(bars("2026-10-12 11:00", 60))
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 12:50:00",
                                        stored=bars("2026-10-12 11:00", 60), calendar=calendar)
    worker.run_until_idle()
    assert book(store) is None
    assert [call for call in exchange.calls if call[2] is not None] == []
    # Последний интервал завершённой сессии остаётся под проверкой.
    assert cache.expected_live_open(cache._key(ITEM, "1m"), pd.Timestamp("2026-10-12 12:50")) == pd.Timestamp("2026-10-12 11:59")

    exchange2 = Exchange(bars("2026-10-13 11:00", 1))
    cache2, store2, worker2, clock2 = setup(tmp_path / "unknown", exchange2, "2026-10-13 11:10:00",
                                            stored=bars("2026-10-13 11:00", 1), calendar=calendar)
    worker2.detect()
    assert book(store2) is not None  # неизвестное расписание не объявлено перерывом


def test_recent_reconciliation_is_distributed_and_uses_fences(tmp_path):
    exchange = Exchange(bars("2026-10-12 12:00", 2))
    settings = MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30, recent_reconciliation_interval_secs=300)
    cache, store, worker, clock = setup(tmp_path, exchange, "2026-10-12 12:02:00", settings=settings,
                                        stored=bars("2026-10-12 12:00", 2))
    worker.run_until_idle()
    assert exchange.calls == []  # никакого burst сверки при запуске
    # Поздняя корректировка отличается от уже закрытой: сначала конфликт.
    exchange.frame = bars("2026-10-12 12:00", 2, price=101)
    clock[0] += timedelta(seconds=601)
    worker.run_until_idle()
    assert exchange.calls  # сверка последних двух баров выполнена по cadence
    assert cache.frame_for(ITEM, "1m")["close"].iloc[-1] == 101  # согласована через fence


def test_history_error_keeps_pair_not_ready_and_is_visible(tmp_path):
    clock = [pd.Timestamp("2026-10-12 12:05")]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    def loader(**kwargs):
        raise RuntimeError("UNAVAILABLE")
    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0),
                            market_store=MarketDataStore(tmp_path / "market.sqlite3"))
    cache.prepare_live_history(ITEM, "1m", 10)
    worker = IngestionWorker(cache, settings=LEGACY_QUEUE)
    worker.run_until_idle()
    status = cache.pair_status(ITEM, "1m")
    assert status["state"] == "BOOTSTRAPPING" and "UNAVAILABLE" in status["history_error"]
    assert worker.startup_complete()  # попытка была; ожидание старта не бесконечно


def test_saved_obligations_register_management_timeframe_and_execution_lower(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import timezone
    clock = [pd.Timestamp("2026-10-12 12:05")]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), UID), timeline,
                            request_budget=RequestBudget(lambda: clock[0], 0),
                            market_store=MarketDataStore(tmp_path / "market.sqlite3"))
    plan = SimpleNamespace(instrument_id=UID, timeframe="1h", profile=SimpleNamespace(parameters={"atr_period": 20}))
    storage = SimpleNamespace(load_trades=lambda: (SimpleNamespace(plan=plan),),
        execution_bar_boundaries=lambda: {UID: (datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc), False)})
    started = []
    monkeypatch.setattr(core.LiveMarketDataPoller, "start", lambda self: started.append(self))
    core._start_live_ingestion(cache, contour.build_demand(SimpleNamespace(storage=storage), [ITEM], cache), None)
    needs = {key[2]: count for key, _, count in cache.live_needs()}
    assert needs == {"1h": 21, "1m": 1}  # сопровождение не зависит от удалённой привязки
    assert cache._pending_execution_lower[UID] == pd.Timestamp("2026-10-12 11:00")
    assert started


def test_new_start_without_obligations_checks_continuity_from_cutover(tmp_path):
    # Пятничные данные, запуск в понедельник: прошлое брокер не подтверждает.
    exchange = Exchange(bars("2026-10-09 20:00", 30))
    clock = [pd.Timestamp("2026-10-12 07:01:10")]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=bars("2026-10-09 20:00", 30))
    store.save_live_state(RecoveryRange(pd.Timestamp("2026-10-09 20:30"), pd.Timestamp("2026-10-10 10:00"),
        pd.Timestamp("2026-10-10"), pd.Timestamp("2026-10-10"), pd.Timestamp("2026-10-09 20:30")).as_json(),
        kind="recovery", source=SOURCE, instrument_uid=UID, interval="1m")  # область прошлого запуска
    cache = MarketDataCache(exchange.loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0), market_store=store)
    cache.register_live_pair(ITEM, UID, "1m")
    cache.mark_live_history(UID, "1m", done=True)
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30))
    worker.detect()
    assert book(store) is None  # выходные до запуска не ищутся и старая область снята
    assert cache.pair_status(ITEM, "1m")["state"] == "READY"
    assert cache.pair_status(ITEM, "1m")["cutover"] == "2026-10-12T07:01:10"
    clock[0] = pd.Timestamp("2026-10-12 07:03:00")
    worker.detect()
    assert book(store).lower == pd.Timestamp("2026-10-12 07:00")  # после запуска — строго


def test_saved_obligation_keeps_strict_execution_prefix_across_restart(tmp_path):
    exchange = Exchange(bars("2026-10-09 20:00", 30))
    clock = [pd.Timestamp("2026-10-12 07:01:10")]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=bars("2026-10-09 20:00", 30))
    cache = MarketDataCache(exchange.loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0), market_store=store)
    cache.note_execution_lower(ITEM, pd.Timestamp("2026-10-09 20:29"))
    cache.register_live_pair(ITEM, UID, "1m")
    cache.mark_live_history(UID, "1m", done=True)
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=False, candle_publication_timeout_secs=30))
    worker.detect()
    assert book(store).lower == pd.Timestamp("2026-10-09 20:30")  # неизвестные выходные удерживают позицию
    assert cache.pair_status(ITEM, "1m")["cutover"] is None
    assert not cache.execution_ready(ITEM, pd.Timestamp("2026-10-12 07:00"))
