"""Контракт AsyncAPI для сообщений Redis Stream market.events."""

from pathlib import Path

import yaml


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "docs" / "market-events.asyncapi.yaml"


def _document():
    document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert document["asyncapi"] == "3.0.0"
    assert document["channels"]["marketEvents"]["address"] == "market.events"
    return document


def _pattern_schema():
    return _document()["components"]["schemas"]["PatternEvent"]


def test_asyncapi_declares_all_pattern_event_fields_and_versions():
    schema = _pattern_schema()

    assert set(schema["required"]) == {
        "schema_version", "event_id", "ticker", "timeframe", "timestamp",
        "published_at", "source", "event_type", "direction", "payload",
    }
    properties = schema["properties"]
    assert properties["schema_version"]["const"] == 1
    assert properties["event_id"]["minLength"] == 1
    assert properties["source"]["const"] == "candle_patterns"
    assert set(properties["event_type"]["enum"]) == {"bullish_engulfing", "bearish_engulfing"}
    assert set(properties["direction"]["enum"]) == {"long", "short"}
    assert properties["timestamp"]["format"] == "date-time"
    assert properties["published_at"]["format"] == "date-time"


def test_asyncapi_limits_pattern_pairs_and_ohlc_payload():
    schema = _pattern_schema()
    pairs = {
        (branch["properties"]["event_type"]["const"], branch["properties"]["direction"]["const"])
        for branch in schema["oneOf"]
    }

    assert pairs == {("bullish_engulfing", "long"), ("bearish_engulfing", "short")}
    payload = schema["properties"]["payload"]
    assert set(payload["required"]) == {"body_overlap_ratio", "previous_candle", "current_candle"}
    ratio = payload["properties"]["body_overlap_ratio"]
    assert ratio["minimum"] == 0
    assert ratio["maximum"] == 1
    ohlc = _document()["components"]["schemas"]["Ohlc"]
    assert set(ohlc["required"]) == {"open", "high", "low", "close"}
