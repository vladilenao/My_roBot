"""Восстановленное расписание эмулятора: происхождение дней и два вида календаря."""

import copy
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.market_store import MarketDataStore
from src.market_calendar import CANDLE_RULES, MarketPhase
from src.market_calendar.service import EmulatorScheduleSource, MarketCalendar, SnapshotScheduleStore

FIXTURE = Path(__file__).parent / "fixtures" / "reconstructed_schedule_2025.json"
EXCHANGE = "forts_futures_weekend"
RULES = json.loads(FIXTURE.read_text(encoding="utf-8"))["days"][1]["rules_version"]
NOW = datetime(2025, 4, 18, 9, 0)  # 12:00 МСК
TIMEOUT = timedelta(seconds=65)


def fixture_days():
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["days"])


class Provider:
    snapshot_generation = 7

    def __init__(self, payload):
        self.payload = payload

    @contextmanager
    def client_context(self, token=None):
        yield SimpleNamespace(trading_schedules=lambda **_: self.payload)


def payload(days, **extra):
    return {"instrument_id": "uid", "exchange": EXCHANGE, "snapshot_generation": 7,
            "missing_dates": [], "days": days, **extra}


def loaded(days, *, allow=True, **extra):
    store = SnapshotScheduleStore()
    source = EmulatorScheduleSource(store, Provider(payload(days, **extra)))
    calendar = MarketCalendar(store, source, allow_reconstructed=allow)
    calendar.calendar_for("uid", NOW)
    assert calendar.service(NOW)
    return calendar, source, store


def without_provenance(days):
    for day in days:
        for key in ("provenance", "rules_version", "source"):
            day.pop(key, None)
    return days


def test_reconstructed_days_pass_robot_version_check():
    calendar, source, _ = loaded(fixture_days())

    full = calendar.calendar_for("uid", NOW, include_reconstructed=True)

    assert source.errors == {}
    day = full.day(date(2025, 4, 18))
    assert (day.provenance, day.rules_version) == ("reconstructed", RULES)
    assert [w.raw_type for w in day.windows if w.raw_type.endswith(("_main", "_evening"))] == [
        "regular_trading_session_main", "regular_trading_session_evening",
    ]


def test_evidence_view_does_not_see_reconstructed_days():
    calendar, _, _ = loaded(fixture_days())

    assert calendar.calendar_for("uid", NOW) is None
    assert calendar.calendar(EXCHANGE, NOW) is None
    assert calendar.expectation("uid", CANDLE_RULES["1m"], NOW, TIMEOUT).phase is MarketPhase.UNKNOWN


def test_reconstructed_days_are_hidden_when_run_disables_them():
    calendar, _, _ = loaded(fixture_days(), allow=False)

    assert calendar.calendar_for("uid", NOW, include_reconstructed=True) is None


def test_old_emulator_without_provenance_is_broker_schedule():
    calendar, _, store = loaded(without_provenance(fixture_days()))

    evidence = calendar.calendar_for("uid", NOW)

    assert evidence.day(date(2025, 4, 18)).provenance == "broker"
    assert calendar.expectation("uid", CANDLE_RULES["1m"], NOW, TIMEOUT).phase is MarketPhase.TRADING
    assert store.schedule_binding_provenance(source="emulator", instrument_uid="uid") == "broker"


def test_my_robot_export_is_broker_and_wins_in_both_views():
    days = fixture_days()
    days[1]["provenance"] = "my_robot_export"
    days[1].pop("rules_version")
    calendar, _, _ = loaded(days, binding_provenance="reconstructed")

    evidence = calendar.calendar_for("uid", NOW)
    full = calendar.calendar_for("uid", NOW, include_reconstructed=True)

    assert [d.trading_date for d in evidence.days] == [date(2025, 4, 18)]
    assert full.day(date(2025, 4, 18)).provenance == "broker"
    assert full.day(date(2025, 4, 17)).provenance == "reconstructed"


@pytest.mark.parametrize("change", [
    lambda day: day.update(provenance="guess"),
    lambda day: day.pop("rules_version"),
    lambda day: day.update(rules_version=""),
])
def test_bad_provenance_rejects_whole_page(change):
    days = fixture_days()
    change(days[2])

    calendar, source, store = loaded(days)

    assert source.errors["uid"].startswith("schedule_unavailable:")
    assert store.schedule_days(source="emulator", exchange=EXCHANGE) == ()
    assert store.schedule_binding(source="emulator", instrument_uid="uid") is None
    assert calendar.calendar_for("uid", NOW, include_reconstructed=True) is None


def test_unknown_binding_provenance_rejects_page():
    calendar, source, store = loaded(fixture_days(), binding_provenance="guess")

    assert "происхождение привязки" in source.errors["uid"]
    assert store.schedule_days(source="emulator", exchange=EXCHANGE) == ()


def test_live_store_refuses_reconstructed_days(tmp_path):
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    day = SimpleNamespace(date=datetime(2025, 4, 18), is_trading_day=True, intervals=[])

    with pytest.raises(ValueError):
        store.record_schedule_day(source="tbank", exchange=EXCHANGE, day=day, provenance="reconstructed",
                                  rules_version=RULES)
    with pytest.raises(ValueError):
        store.record_schedule_binding(source="tbank", instrument_uid="uid", exchange=EXCHANGE,
                                      provenance="reconstructed")
    assert store.schedule_days(source="tbank", exchange=EXCHANGE) == ()


def test_usage_counts_days_by_provenance():
    days = fixture_days()
    days[0]["provenance"] = "my_robot_export"
    days[0].pop("rules_version")
    calendar, source, _ = loaded(days, binding_provenance="reconstructed")
    source.requested["uid"] = {date(2025, 4, 17), date(2025, 4, 18), date(2025, 4, 20)}

    usage = calendar.usage()

    assert usage["provenance_counts"] == {"broker": 1, "reconstructed": 1}
    assert usage["rules_versions"] == (RULES,)
    assert usage["binding_provenance"] == {EXCHANGE: ("reconstructed",)}
    assert usage["missing_dates"] == ("2025-04-20",)
    assert usage["use_reconstructed"] is True


def test_usage_marks_disabled_reconstructed_days_as_missing():
    calendar, source, _ = loaded(fixture_days(), allow=False)
    source.requested["uid"] = {date(2025, 4, 18)}

    usage = calendar.usage()

    assert usage["provenance_counts"] == {"reconstructed_unused": 1}
    assert usage["missing_dates"] == ("2025-04-18",)


def test_session_view_records_used_reconstructed_day():
    calendar, _, _ = loaded(fixture_days())

    calendar.calendar_for("uid", NOW, include_reconstructed=True)

    assert [item[1:] for item in calendar.usage()["day_versions"]] == [
        ("2025-04-18", fixture_days()[1]["version"], "reconstructed"),
    ]
