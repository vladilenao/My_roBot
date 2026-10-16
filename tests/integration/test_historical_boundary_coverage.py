"""Настоящий historical-клиент и runtime: будущая диагностика не торгует."""

from datetime import datetime
import json
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

import run
from src.api.emulator_client import EmulatorClientProvider
from src.bot import TradingBot
from src.strategies.contracts import Assignment, Decision, SignalType
from src.history.contract_lifecycle import ConstantContractLifecycle
from src.trade_management.intraday_settings import IntradaySettings

START = datetime(2025, 1, 24, 20, 48)
END = datetime(2025, 1, 25, 12)
LAST = datetime(2025, 1, 24, 20, 49)
RIGHT = datetime(2025, 1, 27, 7)


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Exchange:
    def __init__(self, future_price):
        self.calls = []
        self.future_price = future_price
        self.runtime = False
        self.drop_tail = False
        self.events = []
        self.card = {"instrument_id": "contract-1", "figi": "figi-1", "ticker": "NGF5",
            "name": "NG-1.25 Природный газ", "instrument_type": "future", "class_code": "SPBFUT",
            "currency": "rub", "lot": 1, "min_price_increment": 1, "min_price_increment_amount": 1,
            "initial_margin_on_buy": 100, "initial_margin_on_sell": 100,
            "first_trade_date": "2024-12-01T00:00:00Z", "expiration_date": "2025-01-30T00:00:00Z"}

    def __call__(self, url, timeout=None):
        if not isinstance(url, str):
            return Response({"producer_id": "source-1", "target_change_id": 12, "after_id": 12,
                             "synchronized": True, "snapshot_generation": 42})
        parsed = urlparse(url)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((parsed.path, params))
        if parsed.path == "/health":
            return Response({"status": "ok"})
        if parsed.path == "/api/v1/instruments":
            return Response([self.card])
        if parsed.path.startswith("/api/v1/instruments/"):
            return Response(self.card)
        assert parsed.path == "/api/v1/candles"
        assert params["instrument_id"] == "contract-1"
        assert params["snapshot_generation"] == "42"
        if params["interval"] == "1d":
            times = [datetime(2025, 1, 24)]
        else:
            times = list(pd.date_range("2025-01-24 20:00", LAST, freq="1min"))+[RIGHT]
            if self.runtime and self.drop_tail:
                times = [when for when in times if when != LAST]
        left = pd.Timestamp(params["from"])
        upper = pd.Timestamp(params.get("to", "2040-01-01"))
        rows = []
        for value in times:
            when = pd.Timestamp(value)
            if left <= when <= upper:
                price = self.future_price if when == RIGHT else 100
                rows.append({"datetime": when.strftime("%Y-%m-%d %H:%M:%S"), "open": price,
                             "low": price-1, "high": price+4 if when == LAST else price+1,
                             "close": 103 if when == LAST else price, "volume": 100})
        return Response({"candles": rows, "next_cursor": None, "total": len(rows)})


def historical_run(path, monkeypatch, future_price, *, calendar=False, drop_tail=False):
    exchange = Exchange(future_price)
    monkeypatch.setattr("src.api.emulator_client.request.urlopen", exchange)
    monkeypatch.setattr(run, "EmulatorClientProvider", lambda: EmulatorClientProvider("http://local-source"))
    monkeypatch.setattr(run, "select_instruments", lambda **kw: [("NG-1.25", "NGF5", "future", "NG-1.25")])
    monkeypatch.setattr(run, "state_dir_for", lambda *args: path)
    monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(run, "_token_client_context", lambda: pytest.fail("боевой API"))
    monkeypatch.setattr(run, "ACTIVE_TIMEFRAMES", {"1m"})
    monkeypatch.setattr(run, "FUTURE_STRATEGIES", {"NG": [Assignment("entry", "macd_rsi_stoch", "atr_trend", "raw", timeframe="1m")]})
    monkeypatch.setattr(run, "SHARE_STRATEGIES", {})
    monkeypatch.setattr(run, "_strategy_map", lambda: {"macd_rsi_stoch": object()})
    monkeypatch.setattr(run, "_warmup_bars", lambda: 2)
    monkeypatch.setattr(run, "RISK_LIMITS", {"max_qty": 1, "commission": 1, "slippage": 0})
    # Проверяется граница прогона, а не сессии: входы 1m вне правила сессионной защиты.
    monkeypatch.setattr(run, "INTRADAY_SETTINGS", IntradaySettings(enabled=False))

    class Strategy:
        def compute(self, frame):
            return frame

        def decide(self, frame, timeframe=None):
            return Decision(SignalType.BUY, float(frame["close"].iloc[-1]), bar_time=frame["datetime"].iloc[-1])

    monkeypatch.setattr(run, "TradingBot", lambda **kw: TradingBot(**kw, strategy_factory=lambda *a, **k: Strategy()))
    original_build = run._build_runtime

    def build(instruments, cache, bus, directory, **kw):
        exchange.runtime = True
        exchange.drop_tail = drop_tail
        runtime = original_build(instruments, cache, bus, directory, **kw)
        if calendar:
            cutoff = datetime(2025, 1, 25)

            class Calendar(ConstantContractLifecycle):
                automatic = True

                def next_event(self, now):
                    return cutoff if now < cutoff else None

                def before_tick(self, now):
                    cache.set_active_instruments(self.instruments, next_event=self.next_event)
                    if now >= cutoff and not exchange.events:
                        exchange.events.append(now)

                def after_execution(self, now):
                    if now >= cutoff:
                        runtime.trade_manager.drain_contract("NGF5")
                        runtime.trade_manager.dispatch(now)

                def can_increase(self, ticker, now):
                    return now < cutoff

            runtime.contract_lifecycle = Calendar(instruments)
            runtime.trade_manager.contract_lifecycle = runtime.contract_lifecycle
        return runtime

    monkeypatch.setattr(run, "_build_runtime", build)
    code = run._run_history(START, END, 0)
    report = json.loads((path / "report.json").read_text())
    return code, report, exchange


def test_fixed_terminal_pause_keeps_position_and_excludes_diagnostic_price(tmp_path, monkeypatch):
    _, low, first_source = historical_run(tmp_path / "first", monkeypatch, 10)
    _, high, second_source = historical_run(tmp_path / "second", monkeypatch, 1000000)
    for result in (low, high):
        assert result["covered"] and result["terminal_gap"]
        assert result["range"]["end"] == "2025-01-25 12:00"
        assert result["last_processed_bar"] == "2025-01-24 20:49"
        assert result["stop_reason"] == "конец диапазона внутри подтверждённого перерыва"
        assert result["ticks"] == 2
        assert result["missed_bars"] == 910 and result["gaps"] == 1
        assert result["result"]["fills"] == 1 and result["result"]["open_positions"] == ["NG-1.25 1"]
        assert result["boundary_checks"][0]["first_after"] == "2025-01-27T07:00:00Z"
        assert result["boundary_checks"][0]["snapshot_generation"] == 42
    assert low["result"] == high["result"]
    for exchange in (first_source, second_source):
        calls = [params for route, params in exchange.calls if route == "/api/v1/candles"]
        assert all(params["snapshot_generation"] == "42" for params in calls)
        assert any(pd.Timestamp(params["from"]) == END for params in calls)


def test_calendar_control_runs_before_end_without_retroactive_close(tmp_path, monkeypatch):
    _, report, source = historical_run(tmp_path / "calendar", monkeypatch, 1000000, calendar=True)
    assert source.events == [datetime(2025, 1, 25)]
    assert report["covered"] and report["terminal_gap"]
    assert report["result"]["fills"] == 1
    assert report["result"]["open_positions"] == ["NG-1.25 1"]
    assert report["missed_bars"] == 910 and report["gaps"] == 1
    assert report["ticks"] == 2


def test_early_runtime_truncation_is_not_terminal_completion(tmp_path, monkeypatch):
    _, report, _ = historical_run(tmp_path / "truncated", monkeypatch, 1000000, drop_tail=True)
    assert not report["covered"] and not report["terminal_gap"]
    assert report["last_processed_bar"] == "2025-01-24 20:48"
    assert report["result"]["fills"] == 0
