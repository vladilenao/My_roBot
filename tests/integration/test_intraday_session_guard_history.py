"""Сессионная защита в настоящей исторической симуляции (add-intraday-session-guard, задача 5.1).

Настоящие TradingBot, JournalBroker, SQLite и тик за тиком по 1m; подставлено только
расписание брокера за 15–16.10.2026 и стратегия «покупать на каждом баре».
"""

from datetime import timedelta

import pandas as pd
import pytest

import run
from src.bot import TradingBot
from src.bot.run_session import HistoricalRunSession
from src.data.cache import MarketDataCache
from src.events.bus import EventBus
from src.history.report import collect_result
from src.history.selection import HistorySelection
from src.instruments.model import Instrument
from src.portfolio import ContractMeta
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies.contracts import Assignment, Decision, SignalType
from tests.support.session_schedule import SHARES, msk

STOCK = Instrument("SBER", "SBER", "share", "SBER", "share")
BINDINGS = {"SBER": [Assignment("share-sber-1m", "macd_rsi_stoch", "levels_rr", "raw", timeframe="1m")]}


def _source(**kw):
    end = kw.get("end_date") or kw["clock"].now()
    start = kw["start_date"] or end - timedelta(minutes=30)
    stamps = pd.date_range(start, end, freq="1min", inclusive="left")
    return pd.DataFrame({"datetime": stamps, "open": 10.0, "close": 10.0, "low": 9.0, "high": 11.0,
                         "volume": 100}), kw["instrument_id"]


class _BuyEveryBar:
    def compute(self, frame):
        return frame

    def decide(self, frame, timeframe=None):
        return Decision(SignalType.BUY, float(frame["close"].iloc[-1]), bar_time=frame["datetime"].iloc[-1])


class _Context:
    def get_context(self, instrument, timeframe, frame=None):
        return MarketContext(TrendResult(TrendDirection.UP, .8), [SRLevel(6, SRType.SUPPORT, 3, "support")], 10)


def _history(tmp_path, monkeypatch, start, end):
    clock = HistoricalClock(start, end, timedelta(minutes=1), 0, sleeper=lambda _: None)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    cache = MarketDataCache(_source, timeline, clock=clock, freshness_tolerance_bars=2)
    session = HistoricalRunSession(clock)
    selection = HistorySelection((STOCK,), (), ()).planned(start, end, {}, 2)
    session.contract_selection = selection
    monkeypatch.setattr(run, "SHARE_STRATEGIES", BINDINGS)
    monkeypatch.setattr(run, "FUTURE_STRATEGIES", {})
    monkeypatch.setattr(run, "RISK_LIMITS", {"max_qty": 1, "commission": 1, "slippage": 0})
    monkeypatch.setattr(run, "TRADE_MANAGEMENT_PROFILES", {"levels_rr": {
        "target_R": [3, 4], "shares": [.5, .5], "min_stop_atr": 0,
        "min_stop_ticks": 1, "stop_beyond_bar": 0, "max_stop_atr": None}})
    monkeypatch.setattr(run, "trading_enabled", lambda: False)
    monkeypatch.setattr(run, "_token_client_context", lambda: pytest.fail("История обратилась к боевому API"))
    monkeypatch.setattr(run, "_load_contracts_metadata",
                        lambda *args, **kwargs: {"share": ContractMeta("share", 1, 1, 0, 0)})
    monkeypatch.setattr(run, "_session_calendar_provider", lambda data_cache: lambda instrument, now: SHARES)
    runtime = run._build_runtime(selection.instruments, cache, EventBus(), tmp_path, clock=clock, session=session)
    bot = TradingBot(selection.instruments, runtime.bus if hasattr(runtime, "bus") else EventBus(),
                     {"macd_rsi_stoch": object()}, cache, timeline,
                     strategy_factory=lambda *args, **kwargs: _BuyEveryBar(), share_strategies=BINDINGS,
                     trade_manager=runtime.trade_manager, action_executor=runtime.action_executor,
                     context_cache=_Context(), post_tick=runtime.post_tick, run=session,
                     contract_lifecycle=runtime.contract_lifecycle)
    try:
        bot.run()
        connection = runtime.storage.connection
        trades = connection.execute("SELECT trade_id, phase FROM trades").fetchall()
        closes = connection.execute(
            "SELECT b.payload_json, f.executed_at FROM orders o JOIN outbox b ON b.command_id=o.command_id "
            "JOIN fills f ON f.order_id=o.order_id WHERE o.action_type='CLOSE'").fetchall()
        rejections = [row[0] for row in connection.execute(
            "SELECT reason FROM calculations WHERE algorithm='session.intraday' AND outcome='REJECTED'")]
        return collect_result(runtime.storage), trades, closes, rejections
    finally:
        runtime.storage.close()


def test_evening_signals_open_nothing(tmp_path, monkeypatch):
    result, trades, _, rejections = _history(tmp_path, monkeypatch, msk(15, 23, 0), msk(15, 23, 15))
    assert trades == [] and result.fills == 0
    assert rejections and set(rejections) == {"intraday-session-closed"}


def test_position_without_first_target_is_closed_before_main_session_end(tmp_path, monkeypatch):
    result, trades, closes, rejections = _history(tmp_path, monkeypatch, msk(16, 17, 50), msk(16, 18, 45))
    assert [phase for _, phase in trades] == ["CLOSED"]
    ((payload, executed_at),) = closes
    assert '"intraday-session-end"' in payload
    assert pd.Timestamp(executed_at).tz_convert(None) >= pd.Timestamp(msk(16, 18, 35))
    assert result.open_positions == []
    assert "intraday-close-near" in rejections
