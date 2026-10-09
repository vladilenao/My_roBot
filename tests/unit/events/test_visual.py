from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from src.events.event import Event
from src.events.visual import bounded_history, market_snapshot, validate, visual_schema


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
