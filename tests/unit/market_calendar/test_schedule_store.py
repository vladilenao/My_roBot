from datetime import datetime, timezone

from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

from src.data.market_store import MarketDataStore


def _day(day: int, *, end_hour: int = 20) -> TradingDay:
    start = datetime(2026, 10, day, 7, tzinfo=timezone.utc)
    end = datetime(2026, 10, day, end_hour, tzinfo=timezone.utc)
    return TradingDay(
        date=datetime(2026, 10, day, tzinfo=timezone.utc),
        is_trading_day=True,
        intervals=[TradingInterval(type="regular_trading_session_main", interval=TimeInterval(start_ts=start, end_ts=end))],
    )


def test_schedule_day_is_versioned_only_when_raw_content_changes(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    first = _day(13)

    assert store.record_schedule_day(source="tbank", exchange="MOEX", day=first, observed_at="2026-10-12T12:00:00Z")
    assert not store.record_schedule_day(source="tbank", exchange="MOEX", day=first, observed_at="2026-10-13T12:00:00Z")

    persisted = store.schedule_days(source="tbank", exchange="MOEX")
    assert len(persisted) == 1
    assert persisted[0].schedule_change_id == 1
    assert persisted[0].observed_at == "2026-10-12T12:00:00Z"
    assert persisted[0].last_confirmed_at == "2026-10-13T12:00:00Z"

    assert store.record_schedule_day(source="tbank", exchange="MOEX", day=_day(13, end_hour=19), observed_at="2026-10-13T13:00:00Z")

    persisted = store.schedule_days(source="tbank", exchange="MOEX")
    assert len(persisted) == 1
    assert persisted[0].schedule_change_id == 2
    assert persisted[0].observed_at == "2026-10-13T13:00:00Z"


def test_schedule_binding_changes_only_when_exchange_changes(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")

    assert store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange="MOEX", observed_at="2026-10-12T12:00:00Z")
    assert not store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange="MOEX", observed_at="2026-10-13T12:00:00Z")
    assert store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange="FORTS", observed_at="2026-10-14T12:00:00Z")

    assert store.schedule_binding(source="tbank", instrument_uid="uid") == "forts"  # код без учёта регистра
    assert [item.schedule_change_id for item in store.schedule_days(source="tbank", exchange="MOEX")] == []
