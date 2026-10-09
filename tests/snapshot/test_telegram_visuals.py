"""Data-driven визуальный контракт; expected facts написаны независимо от renderer."""
import json
import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.events.types import EventType
from src.events.visual import VisualSnapshot, market_snapshot, serializable, utc
from src.notifier.telegram_chart import build_scene, render_png
from src.notifier.telegram_html import visible_text
from src.notifier.telegram_templates import render
from tests.support.telegram import demo_event

CASE = Path(__file__).parent / "data" / "SBER_15m"
CASES = json.loads((CASE / "telegram_cases.json").read_text())["cases"]


def scenario(case):
    event = demo_event(case["stage"], side=case.get("side", "BUY"), quantity=case.get("quantity", 2))
    visual = serializable(event.get("visual"))
    frame = pd.read_csv(CASE / "candles.csv", parse_dates=["datetime"])
    if case.get("side") == "SELL":
        old_high = frame["high"].copy()
        frame["open"], frame["close"] = 600-frame["open"], 600-frame["close"]
        frame["high"], frame["low"] = 600-frame["low"], 600-old_high
    variant = case.get("variant")
    event_type = event.type
    if variant in {"no-targets", "trailing"}:
        visual["plan"].pop("reward_amount", None)
        visual["plan"].pop("net_reward_amount", None)
        if variant == "no-targets":
            visual["plan"]["targets"] = []
        else:
            visual["plan"].update(trailing_quantity=1, fixed_reward_amount="34")
            visual["plan"]["targets"] = visual["plan"]["targets"][:1]
    if variant == "partial-entry":
        visual["fills"][0]["quantity"] = 1
        visual["state"]["quantity"] = 1
    if variant == "partial-target":
        visual["fills"][-1]["quantity"] = 1
        visual["state"]["quantity"] = 2
        visual["state"]["targets"][0]["filled"] = 1
        visual["financial"].update(gross="34", fees="6", net="28")
    if variant == "add":
        visual["fills"].insert(1, {"key": "add", "time": "2026-10-06T07:45:00", "role": "ADD", "side": "BUY", "quantity": 1, "price": "301"})
        visual["state"].update(quantity=2, average_entry="300.3333333333333333333333333")
    if variant == "target-stop":
        visual["fills"][-1].update(role="STOP", price="297.25")
        visual["state"]["targets"][-1]["filled"] = 0
        visual["financial"].update(gross="6.5", fees="6", net="0.5")
        event_type = EventType.STOP_HIT
    if variant == "reduce":
        visual["fills"][-1]["role"] = "REDUCE"
        event_type = EventType.TRADE_CLOSED
    if variant == "long":
        frame = pd.concat([frame.assign(datetime=frame["datetime"]+timedelta(days=day)) for day in range(6)])
        visual["as_of"] = (frame["datetime"].iloc[-1]+timedelta(minutes=15)).isoformat()
    if variant == "gap":
        frame = frame.drop(index=[14, 15])
    visual["market"] = market_snapshot(frame, "15m", utc(visual["as_of"]))
    return replace(event, type=event_type, payload={**event.payload, "status": "partial" if variant in {"partial-entry", "partial-target"} else "fill",
                                                   "visual": VisualSnapshot(visual)})


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_scene_and_text_snapshot(case):
    event = scenario(case)
    expected = case["expected"]
    scene = build_scene(event, 3)
    assert (scene["stage"] if scene else None) == expected["stage"]
    text = visible_text(render(event, tz_offset_hours=3))
    for needle in expected.get("needles", ()):
        assert needle.lower() in text.lower()
    for needle in expected.get("absent", ()):
        assert needle not in text
    if scene:
        assert len(scene["candles"]) == expected["candles"]
        assert len(scene["markers"]) == expected["markers"]
        assert len(scene["before_window"]) == expected.get("before_window", 0)
        if "result" in expected:
            assert scene["result"] == expected["result"]
        # No future bar; true input gaps never get artificial candles.
        for candle in scene["candles"]:
            assert utc(candle["time"]) + timedelta(minutes=15) <= utc(scene["as_of"])
        assert render_png(scene).startswith(b"\x89PNG")
    assert not re.search(r"\b(?:LIMIT|SL|TP1|TP2|BE|Gross|Net|PnL)\b", text)


def test_independent_complete_financial_reference():
    event = scenario(next(c for c in CASES if c["id"] == "approved-final"))
    gross = (Decimal("303.4")-300)*10 + (Decimal("306.4")-300)*10
    fees = Decimal("1.5")*(2+1+1)
    assert gross == 98 and fees == 6 and gross-fees == 92
    finance = event.get("visual")["financial"]
    assert Decimal(finance["gross"]) == gross and Decimal(finance["net"]) == gross-fees


def test_render_review_artifacts(tmp_path):
    import struct
    for stage in ("plan", "stop", "final"):
        event = scenario({"stage": stage})
        png = render_png(build_scene(event, 3))
        assert struct.unpack(">II", png[16:24]) == (1440, 960)
        path = tmp_path / f"telegram-{stage}.png"
        path.write_bytes(png)
        print(path)
