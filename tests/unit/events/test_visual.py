from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from src.events.event import Event
from src.events.visual import breaks_of, bounded_history, market_snapshot, validate, visual_schema


def test_event_deeply_detaches_visual_collections():
    data = {"levels": [{"price": Decimal("0.00000001234567890123456789")}]}
    event = Event.signal("SBER", side="BUY", quantity=2, entry=1, stop=0.9, visual=data)
    data["levels"][0]["price"] = 2
    assert event.to_dict()["payload"]["visual"]["levels"][0]["price"] == "0.00000001234567890123456789"
    with pytest.raises(TypeError):
        event.get("visual")["levels"][0]["price"] = 3


def test_market_excludes_future_invalid_bars_and_marks_gap_and_limit():
    start = datetime(2026, 1, 1)
    times = [start + timedelta(minutes=i) for i in range(100) if i != 95]
    frame = pd.DataFrame({"datetime": times, "open": 10, "low": 9, "high": 12, "close": 11})
    market = market_snapshot(frame, "1m", start + timedelta(minutes=99))
    assert len(market["candles"]) == 80
    assert market["candles"][-1]["time"] == (start + timedelta(minutes=98)).isoformat()
    assert market["gaps"] and market["limited"]
    frame.loc[frame.index[-2], "close"] = 999
    assert market["candles"][-1]["close"] == Decimal(11)


def test_history_compacts_without_losing_quantities_or_weighted_prices():
    fills = [{"key": str(i), "time": f"2026-01-01T00:{i % 60:02}:00", "role": "OPEN", "side": "BUY",
              "quantity": 2, "price": Decimal(i + 1)} for i in range(70)]
    market = {"candles": (), "limited": True, "gaps": False}
    details, groups, stops, result = bounded_history(fills, [], market)
    assert len(details) == 64 and not stops
    assert sum(f["quantity"] for f in details) + sum(g["quantity"] for g in groups) == 140
    assert groups[0]["price"] == Decimal("3.5") and groups[0]["count"] == 6
    assert result["history_limited"]


def test_visual_schema_rejects_nested_drift_and_overlong_history():
    schema = visual_schema()["properties"]["market"]
    data = {"candles": [], "limited": False, "gaps": False, "history_limited": False, "stop_history_limited": False}
    validate(data, schema)
    with pytest.raises(ValueError):
        validate({**data, "unknown": 0}, schema)
    with pytest.raises(ValueError):
        validate({**data, "candles": [{}] * 81}, schema)


def _bars(*stamps):
    return pd.DataFrame({"datetime": pd.to_datetime(stamps), "open": 10, "low": 9, "high": 12, "close": 11})


def test_market_keeps_classified_breaks_and_marks_only_incomplete_ones():
    frame = _bars("2026-10-14 20:30", "2026-10-14 20:45", "2026-10-15 03:45", "2026-10-15 04:00")
    night = (pd.Timestamp("2026-10-14 20:45"), pd.Timestamp("2026-10-15 03:45"))
    market = market_snapshot(frame, "15m", datetime(2026, 10, 15, 4, 15), {night: {"kind": "closed", "missing": 27}})
    assert market["breaks"] == ({"after": "2026-10-14T20:45:00", "before": "2026-10-15T03:45:00",
                                 "kind": "closed", "missing": 27},)
    assert not market["gaps"]
    missing = market_snapshot(frame, "15m", datetime(2026, 10, 15, 4, 15), {night: {"kind": "missing", "missing": 27}})
    assert missing["gaps"]


def test_unclassified_break_is_schedule_unavailable_with_grid_count():
    market = market_snapshot(_bars("2026-10-14 20:45", "2026-10-15 03:45"), "15m", datetime(2026, 10, 15, 4))
    assert market["breaks"] == ({"after": "2026-10-14T20:45:00", "before": "2026-10-15T03:45:00",
                                 "kind": "schedule_unavailable", "missing": 27},)
    assert market["gaps"]


def test_breaks_outside_window_or_after_future_cut_are_dropped():
    start = datetime(2026, 1, 1)
    times = [start + timedelta(minutes=i) for i in range(100) if i not in (5, 98)]
    known = {(start + timedelta(minutes=4), start + timedelta(minutes=6)): {"kind": "missing", "missing": 1}}
    market = market_snapshot(_bars(*times), "1m", start + timedelta(minutes=98), known)
    # Разрыв 4→6 вне окна из 80 свечей, свеча 99 будущая: разрыва 97→99 нет.
    assert market["breaks"] == () and not market["gaps"]


def test_snapshot_without_breaks_field_is_valid_and_derives_unavailable_breaks():
    schema = visual_schema()["properties"]["market"]
    old = {"candles": [{"time": "2026-10-14T20:45:00", "open": "1", "high": "1", "low": "1", "close": "1"},
                       {"time": "2026-10-15T03:45:00", "open": "1", "high": "1", "low": "1", "close": "1"}],
           "limited": False, "gaps": True, "history_limited": False, "stop_history_limited": False}
    validate(old, schema)
    assert [(b["kind"], b["missing"]) for b in breaks_of(old, "15m")] == [("schedule_unavailable", 27)]
    with pytest.raises(ValueError):
        validate({**old, "breaks": [{"after": "a", "before": "b", "kind": "night", "missing": 1}]}, schema)
    with pytest.raises(ValueError):
        validate({**old, "breaks": [{"after": "a", "before": "b", "kind": "closed", "missing": 0}]}, schema)
