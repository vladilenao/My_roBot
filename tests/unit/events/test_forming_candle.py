"""Незакрытая свеча на графике исполнения (fix-trade-close-accuracy).

Песочница 10.11, GOLD-12.26 15m: обе цели исполнились внутри свечи 12:00 МСК,
карточка закрытия ушла в 12:02:21, и график кончался свечой 11:45.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from src.data.cache import MarketDataCache
from src.events.visual import serializable, with_forming_candle
from src.notifier.telegram_chart import build_scene, render_png
from src.notifier.telegram_templates import chart_notes
from src.trade_management.manager import TradeManager
from tests.support.telegram import demo_event

D = Decimal
AS_OF = datetime(2026, 11, 10, 9, 2, 21)          # 12:02:21 МСК


def _candle(time, o, h, low, c):
    return {"time": time, "open": D(o), "high": D(h), "low": D(low), "close": D(c)}


WINDOW = (_candle("2026-11-10T08:30:00", "4155.6", "4159.2", "4154.9", "4158.6"),
          _candle("2026-11-10T08:45:00", "4158.8", "4159.4", "4154.2", "4154.5"))
MINUTES = [
    {"datetime": pd.Timestamp("2026-11-10 08:59"), "open": 4155.7, "high": 4155.7, "low": 4154.2, "close": 4154.5},
    {"datetime": pd.Timestamp("2026-11-10 09:00"), "open": 4154.1, "high": 4154.1, "low": 4135.9, "close": 4140.7},
    {"datetime": pd.Timestamp("2026-11-10 09:01"), "open": 4140.8, "high": 4140.8, "low": 4128.8, "close": 4137.1},
    {"datetime": pd.Timestamp("2026-11-10 09:02"), "open": 4137.0, "high": 4143.4, "low": 4135.1, "close": 4141.0},
]


def _market(candles=WINDOW):
    return {"candles": candles, "breaks": (), "limited": True, "gaps": False,
            "history_limited": False, "stop_history_limited": False}


def test_targets_candle_is_built_from_closed_minutes():
    market = with_forming_candle(_market(), "15m", MINUTES, AS_OF)
    forming = market["candles"][-1]
    assert forming["time"] == "2026-11-10T09:00:00"
    assert (forming["open"], forming["high"], forming["low"], forming["close"]) == (
        D("4154.1"), D("4154.1"), D("4128.8"), D("4137.1"))        # минутка 09:02 ещё не закрыта
    assert forming["through"] == "2026-11-10T09:02:00"
    assert market["candles"][:-1] == WINDOW and market["breaks"] == () and not market["gaps"]
    assert "свеча 12:00 не закрыта: по 12:02" in chart_notes({"market": market, "timeframe": "15m"}, 3)


def test_no_closed_minute_after_window_no_candle():
    market = _market()
    assert with_forming_candle(market, "15m", MINUTES, datetime(2026, 11, 10, 9, 0, 34)) is market
    assert with_forming_candle(market, "15m", [], AS_OF) is market
    assert chart_notes({"market": market, "timeframe": "15m"}, 3) == ["окно: 2 свечи с 10.11 11:30"]


def test_window_stays_within_limit():
    window = tuple(_candle(f"2026-11-10T{h:02d}:{m:02d}:00", "1", "2", "1", "1")
                   for h in range(0, 24) for m in (0, 15, 30, 45))[:80]
    last = window[-1]["time"]
    minute = pd.Timestamp(last) + pd.Timedelta(minutes=15)
    market = with_forming_candle(_market(window), "15m",
                                 [{"datetime": minute, "open": 1, "high": 2, "low": 1, "close": 2}],
                                 minute.to_pydatetime() + pd.Timedelta(minutes=1))
    assert len(market["candles"]) == 80 and market["candles"][0] == window[1] and market["limited"]


def test_cache_closed_minutes_reads_memory_only():
    frame = pd.DataFrame([{**row, "is_complete": True} for row in MINUTES])
    fake = SimpleNamespace(_lock=threading.RLock(), _frames={("GDZ6", "future", "1m"): frame},
                           _key=lambda instrument, tf: (instrument.ticker, instrument.instrument_type, tf))
    gold = SimpleNamespace(ticker="GDZ6", instrument_type="future")
    rows = MarketDataCache.closed_minutes(fake, gold, datetime(2026, 11, 10, 9, 0), AS_OF)
    assert [row["datetime"] for row in rows] == [pd.Timestamp("2026-11-10 09:00"), pd.Timestamp("2026-11-10 09:01")]
    other = SimpleNamespace(ticker="SRZ6", instrument_type="future")
    assert MarketDataCache.closed_minutes(fake, other, datetime(2026, 11, 10, 9, 0), AS_OF) == []
    assert fake._frames.keys() == {("GDZ6", "future", "1m")}       # неизвестная пара не регистрируется


def test_manager_adds_forming_candle_and_survives_source_failure():
    calls = []

    def source(instrument, since, until):
        calls.append((instrument, since, until))
        return MINUTES

    manager = SimpleNamespace(_minute_source=source, _visual_instruments={"GDZ6": "gold"})
    market = TradeManager._with_forming_candle(manager, "GDZ6", "15m", _market(), AS_OF)
    assert market["candles"][-1]["through"] == "2026-11-10T09:02:00"
    assert calls == [("gold", datetime(2026, 11, 10, 9, 0), AS_OF)]

    def broken(*args):
        raise RuntimeError("boom")

    manager._minute_source = broken
    assert TradeManager._with_forming_candle(manager, "GDZ6", "15m", _market(), AS_OF) == _market()


def test_final_chart_renders_forming_candle():
    event = demo_event("final", side="SELL")
    visual = serializable(event.get("visual"))
    last = visual["market"]["candles"][-1]
    start = pd.Timestamp(last["time"]) + pd.Timedelta(minutes=15)
    visual["market"]["candles"].append({"time": start.isoformat(), "open": last["close"], "high": last["high"],
                                        "low": last["low"], "close": last["close"],
                                        "through": (start + pd.Timedelta(minutes=2)).isoformat()})
    scene = build_scene(replace(event, payload={**event.payload, "visual": visual}), 3)
    assert any("не закрыта" in note for note in scene["notes"])
    assert render_png(scene).startswith(b"\x89PNG")
