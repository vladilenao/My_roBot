"""Acceptance Matrix design add-trading-calendar-expectations: сценарии 1–19.

Расписание — реальные ответы брокера 13.10.2026 (фикстура пробы 8.5):
SBER/GAZP — ``moex_mrng_evng_e_wknd_dlr``, NGZ6 — ``forts_futures_weekend``.
Часы управляемые, источники фиктивные, сеть не используется.
"""

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings, candle_publication_timeout
from src.data.live_state import LivePairState
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler, _calendar_from_response
from src.data.request_budget import RequestBudget
from src.history.preflight import Pair, check_right_boundary
from src.instruments import Instrument
from src.market_calendar import (
    CANDLE_RULES, CalendarDay, MarketPhase, SessionWindow, TradingCalendar, expect, expected_bars,
    scheduled_closed_eligible, windows_for_day,
)
from src.market_calendar.clearing import clearing_divergence
from src.market_calendar.service import (
    BrokerScheduleSource, EmulatorScheduleSource, MarketCalendar, SnapshotScheduleStore,
)
from src.scheduler.timing import MultiTimeframeScheduler

FIXTURE = Path(__file__).parent / "fixtures" / "trading_schedules_20261013.json"
SHARES = "moex_mrng_evng_e_wknd_dlr"
FUTURES = "forts_futures_weekend"
TIMEOUT = timedelta(seconds=65)
OBSERVED = datetime(2026, 10, 13, 9, 0)
SOURCE = "tbank_exchange"


def _parse(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc) if isinstance(value, str) else value


def sdk_day(exchange, day):
    entry = json.loads(FIXTURE.read_text(encoding="utf-8"))[exchange]["days"][day]
    named = {name: _parse(value) for name, value in entry["named_fields"].items()}
    intervals = [TradingInterval(type=item["type"], interval=TimeInterval(
        start_ts=_parse(item["start"]), end_ts=_parse(item["end"]))) for item in entry["intervals"]]
    return TradingDay(**named, intervals=intervals)


def fixture_calendar(exchange, *days, extra=()):
    parsed = [CalendarDay(date.fromisoformat(day), True, windows_for_day(sdk_day(exchange, day)), f"v-{day}", OBSERVED)
              for day in days]
    return TradingCalendar("tbank", exchange, tuple(parsed) + tuple(extra))


def utc(text):
    return datetime.fromisoformat(text)


class StaticSource:
    """Источник без сети: дни уже сохранены (как после прошлых наблюдений)."""

    source = "tbank"

    def __init__(self):
        self.requests, self.errors = [], {}

    def request(self, instrument_uid, now, *, force=False):
        self.requests.append((instrument_uid, now, force))

    def service(self, now):
        return False

    def stale(self, exchange, now):
        return False


def stored_calendar(tmp_path, exchange, days, *, uid="uid", observed=OBSERVED, max_age=129600):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_schedule_binding(source="tbank", instrument_uid=uid, exchange=exchange, observed_at=observed)
    for day in days:
        store.record_schedule_day(source="tbank", exchange=exchange, day=sdk_day(exchange, day), observed_at=observed)
    return store, MarketCalendar(store, StaticSource(), schedule_max_age_secs=max_age)


def candles(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps), "open": [100] * len(stamps), "high": [101] * len(stamps),
                         "low": [99] * len(stamps), "close": [100] * len(stamps), "volume": [1] * len(stamps),
                         "is_complete": [True] * len(stamps)})


def live_owner(tmp_path, now, calendar_days, stored, *, exchange=FUTURES, loader=None):
    clock = [pd.Timestamp(now)]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    budget = RequestBudget(lambda: clock[0], 0)
    # Работающий робот подтверждает расписание каждый час: наблюдение свежее.
    store, market_calendar = stored_calendar(tmp_path, exchange, calendar_days, observed=clock[0].to_pydatetime())
    reconciler = MarketDataReconciler(store, source=SOURCE, request_budget=budget, market_calendar=market_calendar)
    calls = []

    def default_loader(**kwargs):
        calls.append((kwargs["start_date"], kwargs.get("end_date")))
        return candles(), "uid"

    cache = MarketDataCache(loader or default_loader, timeline, request_budget=budget, market_store=store,
                            market_reconciler=reconciler, live_cutover=pd.Timestamp(stored[0]))
    item = Instrument("NGZ6", "NGZ6", "future", "NGZ6", "uid")
    store.record_candles(source=SOURCE, instrument_uid="uid", interval="1m", candles=candles(*stored))
    cache.register_live_pair(item, "uid", "1m")
    cache.mark_live_history("uid", "1m", done=True)
    return cache, store, reconciler, item, calls, clock


# 1 ─ запуск в понедельник 03:00 МСК после выходных
def test_01_monday_night_last_weekend_bar_received_is_current_and_waits_for_known_open():
    schedule = fixture_calendar(FUTURES, "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19")
    now = utc("2026-10-19 00:00")  # 03:00 МСК понедельника

    result = expect(schedule, CANDLE_RULES["1m"], now, TIMEOUT)

    assert result.phase is MarketPhase.CLOSED
    # У FORTS_FUTURES_WEEKEND последняя сессия — воскресная 07:00–16:00 UTC.
    assert result.last_due.open == utc("2026-10-18 15:59")
    assert (result.next_event.kind, result.next_event.at) == ("session_open", utc("2026-10-19 03:50"))
    state = LivePairState("tbank", "uid", "1m").observe(
        now=now, session="waiting_session", expected_bar=result.last_due.close, timeout_secs=65,
        received_bar=result.last_due.close, publication_deadline=result.last_due.publication_deadline)
    # Ожидание следующей сессии, а не задержка: срока публикации нет.
    assert state.data_state == "waiting_bar" and state.expected_bar is None and state.publication_deadline is None


# 2 ─ клиринг
def test_02_clearing_is_break_and_hour_overlapping_trading_stays_expected():
    schedule = fixture_calendar(FUTURES, "2026-10-13", "2026-10-14")
    inside = utc("2026-10-13 21:00")  # вечерний клиринг FORTS 20:50:01–21:30 UTC

    minute = expect(schedule, CANDLE_RULES["1m"], inside, TIMEOUT)
    assert (minute.phase, minute.break_kind) == (MarketPhase.BREAK, "clearing")
    assert scheduled_closed_eligible(minute, True)
    assert expected_bars(schedule, CANDLE_RULES["1m"], utc("2026-10-13 20:50"), utc("2026-10-13 21:30"), TIMEOUT) == ()
    hours = expected_bars(schedule, CANDLE_RULES["1h"], utc("2026-10-13 20:00"), utc("2026-10-13 22:00"), TIMEOUT)
    assert [bar.open for bar in hours] == [utc("2026-10-13 20:00")]  # 20:00–21:00 пересекает торги


# 3 ─ рынок закрылся, последний бар не получен
def test_03_closed_market_does_not_hide_missing_last_session_bar():
    schedule = fixture_calendar(FUTURES, "2026-10-13", "2026-10-14")
    now = utc("2026-10-13 21:40")

    result = expect(schedule, CANDLE_RULES["1m"], now, TIMEOUT)
    assert result.phase is MarketPhase.CLOSED
    assert result.last_due.open == utc("2026-10-13 20:49")  # минута 20:50 не торговая
    state = LivePairState("tbank", "uid", "1m").observe(
        now=now, session="waiting_session", expected_bar=result.last_due.close, timeout_secs=65,
        received_bar=utc("2026-10-13 20:40"), publication_deadline=result.last_due.publication_deadline)
    assert state.data_state == "delayed"
    assert state.publication_deadline == utc("2026-10-13 20:51:05")


# 4 ─ праздник в будний день, рабочая суббота, сессия выходного дня
def test_04_phase_follows_date_schedule_not_weekday():
    holiday = CalendarDay(date(2026, 10, 21), False, (), "v-holiday", OBSERVED)
    schedule = fixture_calendar(FUTURES, "2026-10-17", "2026-10-20", extra=(holiday,))
    saturday = SessionWindow(utc("2026-10-24 07:00"), utc("2026-10-24 12:00"), "continuous", "regular_trading_session")
    working = TradingCalendar("tbank", FUTURES, schedule.days + (CalendarDay(date(2026, 10, 24), True, (saturday,), "v-24", OBSERVED),))

    assert expect(working, CANDLE_RULES["1m"], utc("2026-10-21 10:00"), TIMEOUT).phase is MarketPhase.CLOSED
    assert expect(working, CANDLE_RULES["1m"], utc("2026-10-24 08:00"), TIMEOUT).phase is MarketPhase.TRADING
    assert expect(working, CANDLE_RULES["1m"], utc("2026-10-17 10:00"), TIMEOUT).phase is MarketPhase.TRADING


# 5 ─ расписание неизвестно
def test_05_unknown_schedule_never_reduces_polling_or_gives_evidence(tmp_path):
    store, calendar = stored_calendar(tmp_path, FUTURES, ["2026-10-13"], observed=datetime(2026, 10, 1))
    beyond = calendar.expectation("uid", CANDLE_RULES["1m"], utc("2026-10-13 21:40"), TIMEOUT)  # max-age истёк
    missing = calendar.expectation("other-uid", CANDLE_RULES["1m"], utc("2026-10-13 21:40"), TIMEOUT)

    for result in (beyond, missing):
        assert result.phase is MarketPhase.UNKNOWN and result.reason
        assert not scheduled_closed_eligible(result, True)
    reconciler = MarketDataReconciler(store, source=SOURCE, market_calendar=calendar)
    cache = MarketDataCache(lambda **_: (candles(), "uid"), MultiTimeframeScheduler(["1m"]), market_store=store,
                            market_reconciler=reconciler)
    cache._uids[("uid", "future", "1m")] = "uid"
    # UNKNOWN: прежняя сеточная семантика опроса — ждём каждую минуту.
    assert cache.expected_live_open(("uid", "future", "1m"), utc("2026-10-13 21:40:30")) == utc("2026-10-13 21:39")


# 6 ─ неликвидная минута в торговое время
def test_06_quiet_trading_minute_is_expected_and_never_scheduled_closed(tmp_path):
    store, calendar = stored_calendar(tmp_path, FUTURES, ["2026-10-13"])
    reconciler = MarketDataReconciler(store, source=SOURCE, market_calendar=calendar)
    reconciler.reconcile(instrument_uid="uid", interval="1m", candles=candles("2026-10-13 10:00", "2026-10-13 10:02"),
                         now=utc("2026-10-13 10:05"))

    state = store.coverage_state(source=SOURCE, instrument_uid="uid", interval="1m", open_time="2026-10-13 10:01")
    assert state == "pending"  # дальше политика ingestion: deadline, recovery, GetLastTrades


# 7 ─ аукцион открытия и свеча на его границе
def test_07_opening_auction_is_break_and_received_candle_wins(tmp_path):
    store, calendar = stored_calendar(tmp_path, FUTURES, ["2026-10-13"])
    result = expect(calendar.calendar(FUTURES, OBSERVED), CANDLE_RULES["1m"], utc("2026-10-13 03:55"), TIMEOUT)
    assert (result.phase, result.break_kind) == (MarketPhase.BREAK, "auction")

    reconciler = MarketDataReconciler(store, source=SOURCE, market_calendar=calendar)
    reconciler.reconcile(instrument_uid="uid", interval="1m", candles=candles("2026-10-13 03:49", "2026-10-13 04:00"),
                         now=utc("2026-10-13 04:01"))
    assert store.coverage_state(source=SOURCE, instrument_uid="uid", interval="1m", open_time="2026-10-13 03:55") == "scheduled_closed"
    store.record_candles(source=SOURCE, instrument_uid="uid", interval="1m", candles=candles("2026-10-13 03:59"))
    assert store.coverage_state(source=SOURCE, instrument_uid="uid", interval="1m", open_time="2026-10-13 03:59") == "received"


# 8 ─ неизвестный тип интервала
def test_08_unknown_interval_type_is_trading_with_reason():
    day = sdk_day(FUTURES, "2026-10-13")
    day.intervals.append(TradingInterval(type="weekend_session_new_kind", interval=TimeInterval(
        start_ts=datetime(2026, 10, 13, 21, 40, tzinfo=timezone.utc), end_ts=datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc))))
    schedule = TradingCalendar("tbank", FUTURES, (CalendarDay(date(2026, 10, 13), True, windows_for_day(day), "v", OBSERVED),))

    result = expect(schedule, CANDLE_RULES["1m"], utc("2026-10-13 21:45"), TIMEOUT)
    assert result.phase is MarketPhase.TRADING and "unknown_interval_type" in result.reason


# 9 ─ per-TF timeout
def test_09_daily_publication_timeout_overrides_only_its_timeframe():
    settings = MarketDataSettings(candle_publication_timeout_by_tf={"1d": 900})
    schedule = fixture_calendar(FUTURES, "2026-10-13", "2026-10-14")
    now = utc("2026-10-14 03:00")
    daily = expect(schedule, CANDLE_RULES["1d"], now, timedelta(seconds=candle_publication_timeout(settings, "1d")))
    minute = expect(schedule, CANDLE_RULES["1m"], now, timedelta(seconds=candle_publication_timeout(settings, "1m")))

    assert daily.last_due.publication_deadline - daily.last_due.close == timedelta(seconds=900)
    assert minute.last_due.publication_deadline - minute.last_due.close == timedelta(seconds=65)


# 10 ─ брокер изменил расписание дня
def test_10_changed_day_gets_new_version_and_old_evidence_keeps_its_version(tmp_path):
    store, calendar = stored_calendar(tmp_path, FUTURES, ["2026-10-13"])
    reconciler = MarketDataReconciler(store, source=SOURCE, market_calendar=calendar)
    reconciler.reconcile(instrument_uid="uid", interval="1m", candles=candles("2026-10-13 20:49", "2026-10-14 03:59"),
                         now=utc("2026-10-14 04:01"))
    first = store.schedule_days(source="tbank", exchange=FUTURES)[0].version
    def evidence():
        return [row for row in store.changes_after(store.producer_id, 0, limit=1000)["changes"]
                if row["kind"] == "interval_status"]
    before = {row["interval_status"]["evidence"] for row in evidence()}
    assert any(first in item for item in before)

    changed = sdk_day(FUTURES, "2026-10-13")
    changed.intervals = changed.intervals[:-1]  # без вечернего клиринга
    assert store.record_schedule_day(source="tbank", exchange=FUTURES, day=changed, observed_at=OBSERVED)
    assert len({item.version for item in store.schedule_days(source="tbank", exchange=FUTURES)}) == 1
    latest = store.schedule_days(source="tbank", exchange=FUTURES)[0].version
    assert latest != first
    assert {row["interval_status"]["evidence"] for row in evidence()} == before  # прежние доказательства не переписаны


# 11 ─ перезапуск без сети
def test_11_restart_without_network_uses_saved_versions_until_they_expire(tmp_path):
    store, _ = stored_calendar(tmp_path, FUTURES, ["2026-10-13", "2026-10-14"])

    class Offline(StaticSource):
        def service(self, now):
            raise AssertionError("чтение ожидания не должно ходить в сеть")

    reopened = MarketCalendar(MarketDataStore(store.path), Offline())
    assert reopened.expectation("uid", CANDLE_RULES["1m"], utc("2026-10-13 10:00"), TIMEOUT).phase is MarketPhase.TRADING
    later = reopened.expectation("uid", CANDLE_RULES["1m"], utc("2026-10-20 10:00"), TIMEOUT)
    assert later.phase is MarketPhase.UNKNOWN


# 12 ─ закрытый рынок сокращает свечные запросы
def test_12_closed_market_with_received_last_due_sends_no_fresh_getcandles(tmp_path):
    cache, store, reconciler, item, calls, clock = live_owner(
        tmp_path, "2026-10-18 20:00", ["2026-10-17", "2026-10-18", "2026-10-19"],
        ["2026-10-18 15:58", "2026-10-18 15:59"])
    worker = IngestionWorker(cache)
    for _ in range(5):
        worker.run_until_idle()
        clock[0] += pd.Timedelta(minutes=10)

    assert calls == []  # ни свежего, ни recent: последний бар сессии получен
    wake = worker.next_wakeup_delay(clock[0].to_pydatetime())
    assert wake is not None and wake > 3600  # следующее событие — открытие понедельника


def test_12_unknown_schedule_keeps_polling(tmp_path):
    cache, store, reconciler, item, calls, clock = live_owner(
        tmp_path, "2026-10-25 20:00", ["2026-10-17"], ["2026-10-25 19:58"])
    IngestionWorker(cache).run_until_idle()
    assert calls  # расписания на дату нет — опрос не сокращается


# 13 ─ исторический режим
class EmulatorProvider:
    def __init__(self, payload=None, error=None, generation=42):
        self.payload, self.error, self.snapshot_generation = payload, error, generation

    @contextmanager
    def client_context(self, token=None):
        provider = self

        def trading_schedules(**_):
            if provider.error is not None:
                raise provider.error
            return provider.payload

        yield SimpleNamespace(trading_schedules=trading_schedules)


def test_13_snapshot_schedule_is_applied_and_reported(tmp_path):
    from src.market_calendar.serialization import canonical_day_json, day_version
    import run

    day = sdk_day(FUTURES, "2026-10-13")
    payload = {"instrument_id": "uid", "snapshot_generation": 42, "missing_dates": ["2026-10-12"],
               "days": [{"trading_date": "2026-10-13", "exchange": "FORTS_FUTURES_WEEKEND",
                         "version": day_version(day), "day": json.loads(canonical_day_json(day))}]}
    snapshot = SnapshotScheduleStore()
    calendar = MarketCalendar(snapshot, EmulatorScheduleSource(snapshot, EmulatorProvider(payload)))
    now = utc("2026-10-13 21:00")
    assert calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT).phase is MarketPhase.UNKNOWN
    assert calendar.service(now)
    assert calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT).phase is MarketPhase.BREAK

    report = run._calendar_schedule(SimpleNamespace(market_calendar=calendar))
    assert report.snapshot_pinned and report.snapshot_generation == 42
    assert report.exchanges == (FUTURES,)
    assert report.day_versions == ((FUTURES, "2026-10-13", day_version(day), "broker"),)
    # 21:00 UTC — уже 14-е по МСК: его в снимке нет, как и объявленного 12-го.
    assert report.missing_dates == ("2026-10-12", "2026-10-14")


def test_13_emulator_without_resource_is_unknown_and_mismatch_is_an_error():
    from src.api.emulator_client import EmulatorError

    snapshot = SnapshotScheduleStore()
    missing = EmulatorError("NOT_FOUND", "нет маршрута")
    missing.status = 404
    calendar = MarketCalendar(snapshot, EmulatorScheduleSource(snapshot, EmulatorProvider(error=missing)))
    now = utc("2026-10-13 21:00")
    calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)
    assert calendar.service(now)
    result = calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)
    assert result.phase is MarketPhase.UNKNOWN and result.reason == "schedule_resource_unavailable"

    other = SnapshotScheduleStore()
    mismatch = MarketCalendar(other, EmulatorScheduleSource(other, EmulatorProvider(
        error=EmulatorError("SNAPSHOT_MISMATCH", "другой снимок"))))
    mismatch.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)
    with pytest.raises(EmulatorError):
        mismatch.service(now)


# 14 ─ исторический понедельничный cutoff NG
class Source:
    def __init__(self, stamps):
        self.stamps, self.calls = [pd.Timestamp(item) for item in stamps], []

    def __call__(self, **kwargs):
        self.calls.append((kwargs["start_date"], kwargs["end_date"]))
        frame = candles(*[item for item in self.stamps if kwargs["start_date"] <= item < kwargs["end_date"]])
        return frame, kwargs.get("instrument_id")


def test_14_monday_cutoff_is_planned_pause_with_schedule_and_lr_gap_without_it():
    schedule = fixture_calendar(FUTURES, "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19")
    pair = Pair("NG", "future", "1m", "uid", "NG-11.26")
    bars = ["2026-10-18 15:58", "2026-10-18 15:59", "2026-10-19 04:00"]
    boundary = utc("2026-10-19 00:00")

    planned = check_right_boundary(pair, start=utc("2026-10-18 00:00"), boundary=boundary, load_candles=Source(bars),
        calendar_expectation=lambda p, now: expect(schedule, CANDLE_RULES["1m"], now, TIMEOUT))
    assert planned.status == "planned_pause" and planned.ok
    assert planned.schedule_versions and "плановый перерыв подтверждён расписанием" in planned.message()

    observed = check_right_boundary(pair, start=utc("2026-10-18 00:00"), boundary=boundary, load_candles=Source(bars))
    assert observed.status == "confirmed_gap" and not observed.trading_gap


# 15 ─ свеча вне любого окна расписания
def test_15_candle_outside_every_window_is_accepted_and_diagnosed(tmp_path):
    cache, store, reconciler, item, calls, clock = live_owner(
        tmp_path, "2026-10-13 22:10", ["2026-10-13", "2026-10-14"], ["2026-10-13 20:49", "2026-10-13 21:59"])
    reconciler.reconcile(instrument_uid="uid", interval="1m",
                         candles=candles("2026-10-13 20:49", "2026-10-13 21:59"), now=utc("2026-10-13 22:10"))
    assert store.coverage_state(source=SOURCE, instrument_uid="uid", interval="1m", open_time="2026-10-13 21:40") == "scheduled_closed"

    late = candles("2026-10-13 21:40")
    late.attrs["actual_candle_sources"] = {}
    cache.accept_rpc_frame("uid", "1m", late, received_at=utc("2026-10-13 22:10"))

    assert store.coverage_state(source=SOURCE, instrument_uid="uid", interval="1m", open_time="2026-10-13 21:40") == "received"
    note = store.live_state(kind="calendar", source=SOURCE, instrument_uid="uid", interval="1m",
                            open_time=pd.Timestamp("2026-10-13 21:40"))
    assert note.get("outside_schedule") is True
    assert reconciler.calendar_expectation(instrument_uid="uid", interval="1m", now=clock[0]).phase is MarketPhase.CLOSED


# 16 ─ дилерская сессия субботы с 02:00 МСК
def test_16_saturday_dealer_session_starting_on_friday_utc_is_trading():
    schedule = fixture_calendar(SHARES, "2026-10-16", "2026-10-17")

    friday_evening = expect(schedule, CANDLE_RULES["1m"], utc("2026-10-16 20:55"), TIMEOUT)
    assert friday_evening.phase is MarketPhase.CLOSED
    assert (friday_evening.next_event.kind, friday_evening.next_event.at) == ("session_open", utc("2026-10-16 23:00"))

    night = expect(schedule, CANDLE_RULES["1m"], utc("2026-10-16 23:30"), TIMEOUT)
    assert night.phase is MarketPhase.TRADING
    assert night.schedule.days == ((date(2026, 10, 17), "v-2026-10-17"),)
    assert [bar.open for bar in expected_bars(schedule, CANDLE_RULES["1m"], utc("2026-10-16 22:58"),
                                              utc("2026-10-16 23:02"), TIMEOUT)] == [utc("2026-10-16 23:00"), utc("2026-10-16 23:01")]


# 17 ─ одинаковые ожидаемые минуты у календаря и разбора ingestion
@pytest.mark.parametrize("exchange,day", [(SHARES, "2026-10-13"), (FUTURES, "2026-10-13"), (SHARES, "2026-10-17")])
def test_17_calendar_and_ingestion_parser_expect_the_same_minutes(exchange, day):
    raw = sdk_day(exchange, day)
    schedule = TradingCalendar("tbank", exchange, (CalendarDay(date.fromisoformat(day), True, windows_for_day(raw), "v", OBSERVED),))
    session = _calendar_from_response(SimpleNamespace(exchanges=[SimpleNamespace(exchange=exchange.upper(), days=[raw])]), exchange)
    start = datetime.fromisoformat(day) - timedelta(hours=3)
    end = start + timedelta(days=1, hours=3)

    calendar_minutes = {bar.open for bar in expected_bars(schedule, CANDLE_RULES["1m"], start, end, TIMEOUT)}
    parser_minutes = {stamp.to_pydatetime() for stamp in pd.date_range(start, end, freq="1min", inclusive="left")
                      if session.is_open(stamp)}
    assert calendar_minutes == parser_minutes and calendar_minutes


# 18 ─ холодный старт без сохранённого прошлого
def test_18_cold_start_requests_from_now_and_leaves_previous_session_unknown(tmp_path):
    now = utc("2026-10-19 00:00")  # 03:00 МСК понедельника, прошлого расписания нет
    requests = []

    def schedules(*, exchange, from_, to):
        if from_ < now.replace(tzinfo=timezone.utc):
            raise ValueError("30003: `from` can't be less than the current date")
        requests.append((from_, to))
        return SimpleNamespace(exchanges=[SimpleNamespace(exchange=exchange.upper(),
                                                          days=[sdk_day(FUTURES, "2026-10-19"), sdk_day(FUTURES, "2026-10-20")])])

    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(instruments=SimpleNamespace(
                get_instrument_by=lambda **_: SimpleNamespace(instrument=SimpleNamespace(exchange=FUTURES)),
                trading_schedules=schedules))

    store = MarketDataStore(tmp_path / "market.sqlite3")
    calendar = MarketCalendar(store, BrokerScheduleSource(store, Provider(), request_budget=RequestBudget(lambda: now, 0)))
    calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)
    while calendar.service(now):
        calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)

    result = calendar.expectation("uid", CANDLE_RULES["1m"], now, TIMEOUT)
    assert requests and all(start >= now.replace(tzinfo=timezone.utc) for start, _ in requests)
    assert result.phase is MarketPhase.CLOSED and result.last_due is None
    assert "previous_session_unknown" in result.reason
    assert (result.next_event.kind, result.next_event.at) == ("session_open", utc("2026-10-19 03:50"))


# 19 ─ код площадки в другом регистре
def test_19_exchange_code_case_does_not_hide_schedule(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange=SHARES)
    store.record_schedule_day(source="tbank", exchange=SHARES.upper(), day=sdk_day(SHARES, "2026-10-13"), observed_at=OBSERVED)
    calendar = MarketCalendar(store, StaticSource())

    result = calendar.expectation("uid", CANDLE_RULES["1m"], utc("2026-10-13 10:00"), TIMEOUT)
    assert result.phase is MarketPhase.TRADING and result.schedule.exchange == SHARES


def test_clearing_divergence_is_reported_for_forts_without_day_clearing():
    schedule = fixture_calendar(FUTURES, "2026-10-13")
    message = clearing_divergence(schedule.days[0], ["14:05", "19:00"])
    assert message is not None and "23:50–00:30" in message and "14:05, 19:00" in message
    assert clearing_divergence(schedule.days[0], ["23:55"]) is None


def test_status_payload_serializes_real_calendar_objects():
    schedule = fixture_calendar(FUTURES, "2026-10-13", "2026-10-14")
    result = expect(schedule, CANDLE_RULES["1m"], utc("2026-10-13 21:00"), TIMEOUT)
    state = LivePairState("tbank", "uid", "1m", phase=result.phase.value, break_kind=result.break_kind,
                          last_due=result.last_due, next_event=result.next_event, schedule=result.schedule)

    payload = state.payload(utc("2026-10-13 21:00"))
    assert payload["next_event"] == {"kind": "break_end", "at": utc("2026-10-13 21:30")}
    assert payload["schedule"]["source"] == "tbank" and payload["schedule"]["exchange"] == FUTURES
    assert payload["last_due"]["open"] == utc("2026-10-13 20:49")


def test_clearing_divergence_is_logged_once_per_exchange_date_and_version(tmp_path, caplog):
    from src.bot.live_analysis import LiveAnalysis

    store, calendar = stored_calendar(tmp_path, FUTURES, ["2026-10-13", "2026-10-14"])
    now = [utc("2026-10-13 10:00")]
    analysis = LiveAnalysis.__new__(LiveAnalysis)
    analysis.cache = SimpleNamespace(market_calendar=calendar, instrument_uid=lambda instrument, tf: "uid")
    analysis.timeline = SimpleNamespace(now=lambda: now[0])
    analysis.bot = SimpleNamespace(_instruments=[Instrument("NGZ6", "NGZ6", "future", "NGZ6", "uid")] * 2)
    analysis.clearing_times, analysis._clearing_checked = ("14:05", "19:00"), set()

    first = analysis.diagnose_clearing()
    assert len(first) == 1 and "23:50–00:30" in first[0]
    assert analysis.diagnose_clearing() == []  # тот же день и версия — без повторов
    now[0] = utc("2026-10-14 10:00")
    assert len(analysis.diagnose_clearing()) == 1  # новая торговая дата
