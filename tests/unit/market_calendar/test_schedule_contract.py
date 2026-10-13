import json
from datetime import date, datetime, timezone
from pathlib import Path

from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

from src.market_calendar import (
    GENERIC_INTERVAL_TYPES,
    INTERVAL_KIND_TABLE,
    INTERVAL_KIND_TABLE_VERSION,
    interval_kind,
    trading_date,
    windows_for_day,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trading_schedules_20261013.json"
SHARE_EXCHANGE = "moex_mrng_evng_e_wknd_dlr"
FUTURE_EXCHANGE = "forts_futures_weekend"

OBSERVED_KINDS = {
    "regular_trading_session": "continuous",
    "regular_trading_session_morning": "continuous",
    "regular_trading_session_main": "continuous",
    "regular_trading_session_evening": "continuous",
    "dealer_regular_trading_session": "continuous",
    "dealer_holiday_trading_session_morning": "continuous",
    "dealer_holiday_trading_session_evening": "continuous",
    "holiday_trading_session_morning": "continuous",
    "opening_auction": "auction",
    "opening_auction_morning": "auction",
    "holiday_opening_auction_morning": "auction",
    "closing_auction": "auction",
    "closing_auction_main": "auction",
    "clearing": "clearing",
}


def _parse(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc) if isinstance(value, str) else value


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _build_day(entry: dict) -> TradingDay:
    named = {name: _parse(value) for name, value in entry["named_fields"].items()}
    intervals = [
        TradingInterval(type=item["type"], interval=TimeInterval(start_ts=_parse(item["start"]), end_ts=_parse(item["end"])))
        for item in entry["intervals"]
    ]
    return TradingDay(**named, intervals=intervals)


def _day(exchange: str, trading_date_key: str) -> tuple[TradingDay, dict]:
    entry = _load()[exchange]["days"][trading_date_key]
    return _build_day(entry), entry


def test_interval_kind_table_covers_all_observed_types():
    data = _load()
    observed = {
        item["type"]
        for exchange in data.values()
        for day in exchange["days"].values()
        for item in day["intervals"]
    }
    assert observed == set(OBSERVED_KINDS)


def test_interval_kind_classification_matches_broker_data():
    for raw_type, kind in OBSERVED_KINDS.items():
        assert interval_kind(raw_type) == kind
        assert INTERVAL_KIND_TABLE[raw_type] == kind
    assert interval_kind("brand_new_type") == "unknown"
    assert interval_kind("") == "unknown"
    assert INTERVAL_KIND_TABLE_VERSION


def test_unknown_type_is_not_classified_as_break():
    assert interval_kind("mystery_session") == "unknown"
    assert "unknown" not in set(INTERVAL_KIND_TABLE.values())


def test_windows_from_intervals_drop_generic_aliases():
    day, _ = _day(SHARE_EXCHANGE, "2026-10-13")
    windows = windows_for_day(day)

    assert [(w.start.strftime("%H:%M:%S"), w.end.strftime("%H:%M:%S"), w.kind) for w in windows] == [
        ("03:50:00", "03:59:59", "auction"),
        ("04:00:00", "05:59:59", "continuous"),
        ("06:00:00", "15:54:59", "continuous"),
        ("15:55:00", "15:59:30", "auction"),
        ("16:00:01", "20:49:59", "continuous"),
    ]
    assert not any(w.raw_type in GENERIC_INTERVAL_TYPES for w in windows)
    assert not any(w.kind == "clearing" for w in windows)


def test_clearing_is_separate_nontrading_window_for_futures():
    day, _ = _day(FUTURE_EXCHANGE, "2026-10-13")
    windows = windows_for_day(day)

    clearing = [w for w in windows if w.kind == "clearing"]
    assert [(w.start.strftime("%H:%M:%S"), w.end.strftime("%H:%M:%S")) for w in clearing] == [
        ("20:50:01", "21:30:00")
    ]
    assert [w.kind for w in windows if w.start.strftime("%H:%M") == "20:50"] == ["clearing"]


def test_windows_build_from_named_fields_when_intervals_empty():
    day = TradingDay(
        date=datetime(2026, 10, 13, tzinfo=timezone.utc),
        is_trading_day=True,
        start_time=datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 13, 23, 40, tzinfo=timezone.utc),
        opening_auction_start_time=datetime(2026, 10, 13, 6, 50, tzinfo=timezone.utc),
        opening_auction_end_time=datetime(2026, 10, 13, 6, 59, 59, tzinfo=timezone.utc),
        closing_auction_start_time=datetime(2026, 10, 13, 23, 40, tzinfo=timezone.utc),
        closing_auction_end_time=datetime(2026, 10, 13, 23, 49, 59, tzinfo=timezone.utc),
        clearing_start_time=datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc),
        clearing_end_time=datetime(2026, 10, 13, 14, 5, tzinfo=timezone.utc),
        intervals=[],
    )

    windows = windows_for_day(day)

    klyuch = {(w.start.strftime("%H:%M"), w.end.strftime("%H:%M"), w.kind) for w in windows}
    assert klyuch == {
        ("06:50", "06:59", "auction"),
        ("07:00", "14:00", "continuous"),
        ("14:00", "14:05", "clearing"),
        ("14:05", "23:40", "continuous"),
        ("23:40", "23:49", "auction"),
    }


def test_trading_date_has_no_day_shift_for_used_exchanges():
    data = _load()
    for exchange in (SHARE_EXCHANGE, FUTURE_EXCHANGE):
        for key, entry in data[exchange]["days"].items():
            day = _build_day(entry)
            assert day.date.tzinfo == timezone.utc
            assert day.date.hour == 0
            assert trading_date(day) == date.fromisoformat(key)


def test_weekend_dealer_session_crosses_midnight_but_keeps_trading_date():
    day, _ = _day(SHARE_EXCHANGE, "2026-10-17")
    windows = windows_for_day(day)

    assert trading_date(day) == date(2026, 10, 17)
    first = min(windows, key=lambda w: w.start)
    assert first.start.date() == date(2026, 10, 16)
    assert first.kind == "continuous"
    assert any(w.start.date() == date(2026, 10, 17) for w in windows)
