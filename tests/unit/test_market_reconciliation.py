from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import pandas as pd

from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore
from src.data.reconciliation import MarketDataReconciler, SessionCalendar
from src.instruments import Instrument


NOW = pd.Timestamp("2026-10-10 12:05:00")
UID = "uid-1"
SOURCE = "tbank_exchange"


def _frame(*stamps: str) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": [pd.Timestamp(stamp) for stamp in stamps],
        "open": [100.0] * len(stamps), "high": [101.0] * len(stamps),
        "low": [99.0] * len(stamps), "close": [100.5] * len(stamps),
        "volume": [1] * len(stamps), "is_complete": [True] * len(stamps),
    })


class _Timeline:
    def now(self):
        return NOW

    def grid(self, timeframe):
        return SimpleNamespace(
            current_candle_start=lambda value: pd.Timestamp(value).floor("min"),
            next_candle_close=lambda value: pd.Timestamp(value).floor("min") + pd.Timedelta(minutes=1),
        )


def _setup(tmp_path, intervals=(("2026-10-10 12:00", "2026-10-10 13:00"),)):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    reconciler = MarketDataReconciler(store, source=SOURCE)
    reconciler._calendars[UID] = (
        NOW,
        SessionCalendar("MOEX", tuple((pd.Timestamp(a), pd.Timestamp(b)) for a, b in intervals)),
    )
    return store, reconciler


def test_open_session_gap_is_pending_and_late_candle_resolves_it(tmp_path):
    store, reconciler = _setup(tmp_path)
    candles = _frame("2026-10-10 12:00", "2026-10-10 12:02")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=candles)
    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=candles, now=NOW)

    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "pending"
    assert reconciler.recovery_candidate(instrument_uid=UID, interval="1m") == pd.Timestamp("2026-10-10 12:01")

    late = _frame("2026-10-10 12:01")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=late)

    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "received"
    assert reconciler.recovery_candidate(instrument_uid=UID, interval="1m") is None


def test_off_session_interval_is_resolved_without_synthetic_candle(tmp_path):
    _, reconciler = _setup(tmp_path, intervals=(("2026-10-10 12:00", "2026-10-10 12:01"),))

    assert reconciler.interval_state(
        instrument_uid=UID, interval="1m", open_time="2026-10-10 12:02", now=NOW,
    ) == "scheduled_closed"


def test_unknown_schedule_keeps_gap_unresolved(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    reconciler = MarketDataReconciler(store, source=SOURCE)
    reconciler._calendars[UID] = (NOW, None)
    candles = _frame("2026-10-10 12:00", "2026-10-10 12:02")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=candles)
    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=candles, now=NOW)

    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "unresolved"


def test_cache_does_not_send_later_bar_to_simulator_before_gap_resolved(tmp_path):
    store, reconciler = _setup(tmp_path)
    instrument = Instrument("SBER", "SBER", "share")
    cache = MarketDataCache(loader=lambda **_: (pd.DataFrame(), UID), timeline=_Timeline(), market_store=store, market_reconciler=reconciler)
    cache._uids[cache._key(instrument, "1m")] = UID
    store.set_interval_state(
        source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01",
        state="pending", evidence="open_session_missing_candle",
    )
    later = _frame("2026-10-10 12:02")

    assert cache.contiguous_after(instrument, "1m", later, pd.Timestamp("2026-10-10 12:00")).empty

    restored = _frame("2026-10-10 12:01", "2026-10-10 12:02")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=_frame("2026-10-10 12:01"))
    allowed = cache.contiguous_after(instrument, "1m", restored, pd.Timestamp("2026-10-10 12:00"))
    assert list(allowed["datetime"]) == [pd.Timestamp("2026-10-10 12:01"), pd.Timestamp("2026-10-10 12:02")]


def test_oldest_gap_is_recovered_outside_regular_backfill_window(tmp_path):
    store, reconciler = _setup(tmp_path)
    instrument = Instrument("SBER", "SBER", "share")
    calls = []

    def loader(**kwargs):
        calls.append((kwargs["start_date"], kwargs["end_date"]))
        return _frame("2026-10-10 12:01"), UID

    cache = MarketDataCache(
        loader=loader, timeline=_Timeline(), market_store=store,
        market_reconciler=reconciler, data_backfill_window_seconds=60,
    )
    key = cache._key(instrument, "1m")
    cache._uids[key] = UID
    cache._instruments[key] = instrument
    store.set_interval_state(
        source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01",
        state="pending", evidence="open_session_missing_candle",
    )

    recovered, attempted = cache._recover_oldest_gap(key, "1m", NOW)

    assert attempted is True
    assert not recovered.empty
    assert calls == [(pd.Timestamp("2026-10-10 12:01"), pd.Timestamp("2026-10-10 12:02"))]
    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "received"


def test_empty_recovery_response_keeps_interval_unresolved(tmp_path):
    store, reconciler = _setup(tmp_path)
    instrument = Instrument("SBER", "SBER", "share")
    cache = MarketDataCache(
        loader=lambda **_: (pd.DataFrame(), UID), timeline=_Timeline(),
        market_store=store, market_reconciler=reconciler,
    )
    key = cache._key(instrument, "1m")
    cache._uids[key], cache._instruments[key] = UID, instrument
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01", state="pending", evidence="open_session_missing_candle")

    recovered, attempted = cache._recover_oldest_gap(key, "1m", NOW)

    assert attempted is True and recovered.empty
    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "unresolved"


def test_rate_limit_keeps_recovery_candidate_for_retry(tmp_path):
    store, reconciler = _setup(tmp_path)
    instrument = Instrument("SBER", "SBER", "share")
    cache = MarketDataCache(
        loader=lambda **_: (_ for _ in ()).throw(RuntimeError("resource_exhausted: ratelimit_reset=3")),
        timeline=_Timeline(), market_store=store, market_reconciler=reconciler,
    )
    key = cache._key(instrument, "1m")
    cache._uids[key], cache._instruments[key] = UID, instrument
    store.set_interval_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01", state="pending", evidence="open_session_missing_candle")

    _, attempted = cache._recover_oldest_gap(key, "1m", NOW)

    assert attempted is True
    # Соблюдаем и подсказку брокера, и минимальную защитную паузу кэша.
    assert cache._retry_after == NOW + pd.Timedelta(seconds=10)
    assert reconciler.recovery_candidate(instrument_uid=UID, interval="1m") == pd.Timestamp("2026-10-10 12:01")


def test_processed_cursor_advances_only_after_commit_marker(tmp_path):
    store, _ = _setup(tmp_path)
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=_frame("2026-10-10 12:01"))
    before = store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")
    store.mark_processed(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01")
    after = store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")

    assert before["processed_through"] is None
    assert after["received_through"] == after["resolved_through"] == after["processed_through"] == "2026-10-10T12:01:00Z"


def test_reconciler_persists_broker_schedule_and_trading_status(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    # Приостановленный trading_status сохраняется, но не превращает уже
    # ожидаемую минуту в подтверждённое отсутствие сделок.
    status = SimpleNamespace(__annotations__={"trading_status": object}, trading_status="SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING")
    interval = SimpleNamespace(interval=SimpleNamespace(start_ts=pd.Timestamp("2026-10-10 12:00", tz="UTC"), end_ts=pd.Timestamp("2026-10-10 13:00", tz="UTC")))
    schedule = SimpleNamespace(exchange="MOEX", days=[SimpleNamespace(is_trading_day=True, intervals=[interval])])
    client = SimpleNamespace(
        instruments=SimpleNamespace(
            get_instrument_by=lambda **_: SimpleNamespace(instrument=SimpleNamespace(exchange="MOEX")),
            trading_schedules=lambda **_: SimpleNamespace(exchanges=[schedule]),
        ),
        market_data=SimpleNamespace(get_trading_status=lambda **_: status),
    )

    class Provider:
        @contextmanager
        def client_context(self, token):
            yield client

    reconciler = MarketDataReconciler(store, source=SOURCE, client_provider=Provider())
    candles = _frame("2026-10-10 12:00", "2026-10-10 12:02")
    store.record_candles(source=SOURCE, instrument_uid=UID, interval="1m", candles=candles)
    reconciler.reconcile(instrument_uid=UID, interval="1m", candles=candles, now=NOW)

    with sqlite3.connect(store.path) as connection:
        kinds = {row[0] for row in connection.execute("SELECT kind FROM source_observations")}
    assert kinds == {"trading_schedule", "trading_status"}
    assert store.coverage_state(source=SOURCE, instrument_uid=UID, interval="1m", open_time="2026-10-10 12:01") == "pending"
