import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget, RequestDeferred
from src.market_calendar import CANDLE_RULES, MarketPhase
from src.market_calendar.serialization import canonical_day_json, day_version
from src.api.emulator_client import EmulatorError
from src.market_calendar.service import BrokerScheduleSource, EmulatorScheduleSource, MarketCalendar, SnapshotScheduleStore


def test_calendar_read_queues_broker_load_and_service_uses_one_rpc_per_step(tmp_path):
    now = datetime(2026, 10, 13, 12)
    calls = []
    day = TradingDay(
        date=datetime(2026, 10, 13, tzinfo=timezone.utc),
        is_trading_day=True,
        intervals=[
            TradingInterval(
                type="regular_trading_session_main",
                interval=TimeInterval(
                    start_ts=datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
                    end_ts=datetime(2026, 10, 13, 14, tzinfo=timezone.utc),
                ),
            )
        ],
    )

    def binding(**_):
        calls.append("binding")
        # Как у брокера (проба 8.5): в карточке код строчный, в расписании — прописной.
        return SimpleNamespace(instrument=SimpleNamespace(exchange="moex_mrng_evng_e_wknd_dlr"))

    def schedules(*, exchange, from_, to):
        if from_ < now.replace(tzinfo=timezone.utc):
            raise ValueError("30003: `from` can't be less than the current date")
        calls.append("schedule")
        return SimpleNamespace(exchanges=[SimpleNamespace(exchange="MOEX_MRNG_EVNG_E_WKND_DLR", days=[day])])

    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(instruments=SimpleNamespace(
                get_instrument_by=binding,
                trading_schedules=schedules,
            ))

    store = MarketDataStore(tmp_path / "market.sqlite3")
    source = BrokerScheduleSource(
        store, Provider(), request_budget=RequestBudget(lambda: now, 0), refresh_seconds=60
    )
    calendar = MarketCalendar(store, source)

    initial = calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65))
    assert initial.phase is MarketPhase.UNKNOWN
    assert calls == []

    assert calendar.service(now)
    assert calls == ["binding"]
    assert calendar.service(now)
    assert calls == ["binding", "schedule"]

    result = calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65))
    assert result.phase is MarketPhase.TRADING
    assert result.schedule is not None
    assert result.schedule.exchange == "moex_mrng_evng_e_wknd_dlr"

    later = now + timedelta(seconds=61)
    calendar.expectation("uid", CANDLE_RULES["1m"], later, timedelta(seconds=65))
    assert calendar.service(later)
    assert calls == ["binding", "schedule", "schedule"]


def test_expired_day_is_unknown_and_requeues_refresh_without_rpc_on_read(tmp_path):
    now = datetime(2026, 10, 13, 12)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    old_day = TradingDay(date=datetime(2026, 10, 13, tzinfo=timezone.utc), is_trading_day=False, intervals=[])
    store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange="MOEX", observed_at=now)
    store.record_schedule_day(source="tbank", exchange="MOEX", day=old_day, observed_at=datetime(2026, 10, 10))

    class Source:
        source = "tbank"

        def __init__(self):
            self.requests = []

        def request(self, uid, requested_at, *, force=False):
            self.requests.append((uid, requested_at))

        def service(self, requested_at):
            return False

        def stale(self, exchange, requested_at):
            return False

    source = Source()
    calendar = MarketCalendar(store, source, schedule_max_age_secs=3600)

    result = calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65))

    assert result.phase is MarketPhase.UNKNOWN
    assert source.requests == [("uid", now)]


def test_emulator_source_uses_snapshot_endpoint_without_broker_fallback(tmp_path):
    now = datetime(2026, 10, 13, 12)
    day = TradingDay(
        date=datetime(2026, 10, 13, tzinfo=timezone.utc),
        is_trading_day=True,
        intervals=[TradingInterval(
            type="regular_trading_session_main",
            interval=TimeInterval(start_ts=datetime(2026, 10, 13, 10, tzinfo=timezone.utc), end_ts=datetime(2026, 10, 13, 14, tzinfo=timezone.utc)),
        )],
    )
    calls = []

    class Provider:
        @contextmanager
        def client_context(self):
            yield SimpleNamespace(trading_schedules=lambda **_: calls.append("emulator") or {
                "days": [{
                    "exchange": "MOEX", "trading_date": "2026-10-13",
                    "version": day_version(day), "day": json.loads(canonical_day_json(day)),
                }]
            })

    store = MarketDataStore(tmp_path / "market.sqlite3")
    source = EmulatorScheduleSource(store, Provider())
    calendar = MarketCalendar(store, source)

    assert calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65)).phase is MarketPhase.UNKNOWN
    assert calendar.service(now)
    assert calls == ["emulator"]
    assert calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65)).phase is MarketPhase.TRADING


def test_historical_calendar_keeps_missing_snapshot_dates_unknown_without_retrying_404():
    now = datetime(2026, 10, 13, 12)
    calls = []

    class Provider:
        @contextmanager
        def client_context(self):
            yield SimpleNamespace(trading_schedules=lambda **_: calls.append(_) or (_ for _ in ()).throw(
                EmulatorError("NOT_FOUND", "Ресурс не найден", 404)
            ))

    store = SnapshotScheduleStore()
    source = EmulatorScheduleSource(store, Provider())
    calendar = MarketCalendar(store, source)

    assert calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65)).phase is MarketPhase.UNKNOWN
    assert calendar.service(now)
    result = calendar.expectation("uid", CANDLE_RULES["1m"], now, timedelta(seconds=65))

    assert result.phase is MarketPhase.UNKNOWN
    assert result.reason == "schedule_resource_unavailable"
    assert not calendar.service(now)
    assert len(calls) == 1


def test_request_budget_applies_instruments_limit_without_splitting_retry_after():
    clock = [datetime(2026, 10, 13, 12)]
    budget = RequestBudget(lambda: clock[0], 0, per_minute_by_service={"instruments": 1})

    assert budget.call(lambda: "schedule", key="schedule", service="instruments") == "schedule"
    assert budget.call(lambda: "candle", key="candle", service="market_data") == "candle"
    try:
        budget.call(lambda: "binding", key="binding", service="instruments")
    except RequestDeferred:
        pass
    else:
        raise AssertionError("Лимит instruments должен отложить второй RPC")


def test_calendar_rpcs_do_not_wait_for_candle_pacing_and_schedule_goes_first(tmp_path):
    # В бою 13.10 загрузка стояла: запросы календаря занимали 5-секундное окно
    # свечных дозагрузок и ждали расписание режима за десятками привязок.
    now = datetime(2026, 10, 13, 12)
    calls = []
    day = TradingDay(date=datetime(2026, 10, 13, tzinfo=timezone.utc), is_trading_day=True, intervals=[])

    def binding(*, id, **_):
        calls.append(("binding", id))
        return SimpleNamespace(instrument=SimpleNamespace(exchange="forts_futures_weekend"))

    def schedules(*, exchange, **_):
        calls.append(("schedule", exchange))
        return SimpleNamespace(exchanges=[SimpleNamespace(exchange=exchange.upper(), days=[day])])

    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(instruments=SimpleNamespace(get_instrument_by=binding, trading_schedules=schedules))

    store = MarketDataStore(tmp_path / "market.sqlite3")
    budget = RequestBudget(lambda: now, 5)
    calendar = MarketCalendar(store, BrokerScheduleSource(store, Provider(), request_budget=budget))
    for uid in ("a", "b", "c"):
        calendar.expectation(uid, CANDLE_RULES["1m"], now, timedelta(seconds=65))
    budget.call(lambda: None, key="candles")  # свечной запрос занял окно темпа

    assert calendar.service(now)  # привязка a
    assert calendar.service(now)  # расписание режима — раньше привязок b и c
    assert calls == [("binding", "a"), ("schedule", "forts_futures_weekend")]
    while calendar.service(now):
        pass
    assert len(calls) == 4
