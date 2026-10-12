"""Офлайн-проверка read-only пробы 8.5 на фиктивных клиенте и потоке."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from queue import Empty, Queue
from types import SimpleNamespace
import json
import threading

import pandas as pd

import tools.ingestion_probe as probe
from src.api.market_data_stream import CandleSubscriptionAck, StreamCandle, StreamResponse


def quote(value):
    return SimpleNamespace(units=int(value), nano=0)


class MarketData:
    def __init__(self):
        self.calls = []

    def get_candles(self, *, instrument_id, from_, to, interval, candle_source_type=None, limit=None):
        self.calls.append((from_, to, limit))
        step = timedelta(minutes=1)
        start = from_.replace(second=0, microsecond=0)
        times = [start + step * index for index in range(min(int((to - start) / step) + 1, 40))]
        candles = [SimpleNamespace(time=moment, open=quote(100), high=quote(101), low=quote(99), close=quote(100),
                                   volume=1, is_complete=True, candle_source=1) for moment in times if moment <= to]
        return SimpleNamespace(candles=candles[:limit] if limit else candles)

    def get_trading_status(self, *, instrument_id):
        return SimpleNamespace(trading_status=SimpleNamespace(name="SECURITY_TRADING_STATUS_NORMAL_TRADING"),
                               api_trade_available_flag=True)

    def get_last_trades(self, *, instrument_id, from_, to):
        return SimpleNamespace(trades=[1, 2])


class Instruments:
    def get_instrument_by(self, *, id_type, id):
        return SimpleNamespace(instrument=SimpleNamespace(exchange="MOEX", class_code="TQBR"))

    def trading_schedules(self, *, exchange, from_, to):
        interval = SimpleNamespace(type="regular_trading_session",
                                   interval=SimpleNamespace(start_ts=from_, end_ts=from_ + timedelta(hours=8)))
        day = SimpleNamespace(date=from_, is_trading_day=True, start_time=from_, end_time=from_, intervals=[interval])
        return SimpleNamespace(exchanges=[SimpleNamespace(exchange=exchange, days=[day])])


class Session:
    def __init__(self):
        self.responses = Queue()
        self.closed = threading.Event()
        self.requests = []

    def open(self):
        return self

    def configure_ping(self, value):
        self.requests.append(("ping", value))

    def subscribe(self, pairs):
        self.requests.append(("subscribe", tuple(pairs)))
        for uid, tf in pairs:
            self.responses.put(StreamResponse("subscribe_candles_response", acknowledgements=(
                CandleSubscriptionAck(uid, tf, 1, 1, True, None, "s", "sub", "t"),)))
        self.responses.put(StreamResponse("candle", candle=StreamCandle(
            pairs[0][0], "1m", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 12, 12, 0, 30, tzinfo=timezone.utc), (Decimal(100), Decimal(101), Decimal(99), Decimal(100)), 1, 1)))
        self.responses.put(StreamResponse("ping", server_time=datetime(2026, 10, 12, tzinfo=timezone.utc)))

    def subscriptions(self):
        self.requests.append(("check",))
        self.responses.put(StreamResponse("subscribe_candles_response", acknowledgements=(
            CandleSubscriptionAck("uid-SBER", "1m", 1, 1, True, None, "s", "sub", "t"),)))

    def __iter__(self):
        while not self.closed.is_set():
            try:
                yield self.responses.get(timeout=0.02)
            except Empty:
                continue

    def close(self):
        self.closed.set()


def test_probe_collects_all_sections_without_orders(monkeypatch, tmp_path):
    monkeypatch.setattr("src.api.instruments.find_working_instrument", lambda client, ticker, kind="share", **kw: "uid-" + ticker)
    monkeypatch.setattr("src.data.loader.load_candles", lambda *a, **kw: (pd.DataFrame({"datetime": [1, 2, 3]}), "uid"))
    client = SimpleNamespace(market_data=MarketData(), instruments=Instruments())
    sessions = []
    report = probe.run_probe(client, lambda: sessions.append(Session()) or sessions[-1], ["SBER:share"],
                             listen_seconds=1, check_after=0, live_minutes=0, cold_days=1)
    assert report["instruments"]["ok"] and report["instruments"]["items"][0]["uid"] == "uid-SBER"
    stream = report["stream"]
    assert stream["ok"] and stream["pairs"] == 9 and not stream["missing_acks"]
    assert stream["candles"][0]["received_after_close_s"] is not None
    assert stream["membership_reported"] == ["uid-SBER:1m"]
    assert report["edges:SBER"]["ok"] and "upper_bound_inclusive" in report["edges:SBER"]["1m"]
    assert report["schedule:SBER"]["ok"] and report["schedule:SBER"]["current"][0]["days"][0]["intervals"]
    assert report["sources:SBER"]["ok"] and report["cold_start:SBER"]["ok"]
    assert all(kind in {"ping", "subscribe", "check"} for kind, *_ in sessions[0].requests)
    text = json.dumps(report, default=str)
    assert "token" not in text.lower()
    assert "Подписки: подтверждено 9/9" in probe.summary(report)


def test_failing_section_does_not_stop_others(monkeypatch):
    monkeypatch.setattr("src.api.instruments.find_working_instrument", lambda client, ticker, kind="share", **kw: "uid")
    monkeypatch.setattr("src.data.loader.load_candles", lambda *a, **kw: (pd.DataFrame(), "uid"))
    client = SimpleNamespace(market_data=MarketData(), instruments=Instruments())
    def broken():
        raise RuntimeError("UNAVAILABLE")
    report = probe.run_probe(client, broken, ["SBER:share"], listen_seconds=1, check_after=0, live_minutes=0, cold_days=1)
    assert report["stream"]["ok"] is False and "UNAVAILABLE" in report["stream"]["error"]
    assert report["edges:SBER"]["ok"] and report["schedule:SBER"]["ok"]
    assert "Ошибка раздела stream" in probe.summary(report)


def test_live_mode_compares_stream_with_rpc_and_detects_corrections():
    candles = [{"uid": "u", "timeframe": "1m", "open": "2026-10-12T12:00:00+00:00", "ohlcv": [100.0, 101.0, 99.0, 100.0, 1]},
               {"uid": "u", "timeframe": "1m", "open": "2026-10-12T12:00:00+00:00", "ohlcv": [100.0, 102.0, 99.0, 101.0, 2]}]
    result = probe.compare_stream_with_rpc(SimpleNamespace(market_data=MarketData()), candles)
    assert result["compared"] == 1
    assert "u:1m:2026-10-12T12:00:00+00:00" in result["corrections"]
    assert result["mismatches"]  # последняя версия потока отличается от RPC
