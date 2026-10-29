"""Причина отказа фильтра для уведомления о недопуске (change entry-filter-admission)."""

import pandas as pd

from src.decision.filter import SignalFilter
from src.market_context.models import MarketContext, TrendDirection, TrendResult
from src.strategies.contracts import Decision, SignalType
from src.trade_management.audit import TraceOutcome

from tests.unit.decision.test_triple_screen import (
    _downtrend,
    _frame_from_close,
    _neutral_tail,
    _oversold_tail,
    _triple_filter,
    _uptrend,
)


def _decision(signal_type):
    return Decision(signal_type=signal_type, price=100.0, bar_time=pd.Timestamp("2024-03-08 16:00"), event_id="signal-1")


def _context(direction):
    return MarketContext(trend=TrendResult(direction=direction, strength=0.6), sr_levels=[], current_price=100.0)


class TestBasicLevelsReason:
    def test_buy_against_downtrend(self):
        verdict = SignalFilter().judge(_decision(SignalType.BUY), _context(TrendDirection.DOWN), "basic_levels")

        assert verdict.rejected
        assert verdict.decision.signal_type is SignalType.HOLD
        assert verdict.reason == "покупка против нисходящего тренда"
        assert verdict.trace.outcome is TraceOutcome.REJECTED

    def test_sell_against_uptrend(self):
        verdict = SignalFilter().judge(_decision(SignalType.SELL), _context(TrendDirection.UP), "basic_levels")

        assert verdict.reason == "продажа против восходящего тренда"

    def test_passed_signal_has_no_reason(self):
        verdict = SignalFilter().judge(_decision(SignalType.BUY), _context(TrendDirection.FLAT), "basic_levels")

        assert not verdict.rejected
        assert verdict.reason == ""
        assert verdict.decision.signal_type is SignalType.BUY
        assert verdict.trace.outcome is TraceOutcome.ACCEPTED


class TestTripleScreenReason:
    def _judge(self, monkeypatch, frames, signal_type=SignalType.BUY, timeframe="5m"):
        from src.decision.filters import PROFILES

        monkeypatch.setitem(PROFILES, "triple_screen", _triple_filter(frames))
        return SignalFilter().judge(_decision(signal_type), _context(TrendDirection.FLAT), "triple_screen",
                                    instrument="NGV6", timeframe=timeframe)

    def test_screen2_only(self, monkeypatch):
        verdict = self._judge(monkeypatch, {"4h": _frame_from_close(_uptrend()),
                                            "30m": _frame_from_close(_neutral_tail(), freq="30min")})

        assert verdict.trace.reason == "screen2-not-confirmed"
        assert verdict.reason.startswith("экран 2 (Stochastic 30m) не подтверждает покупку: %K ")
        assert verdict.reason.endswith("не ниже 20")
        assert verdict.trace.inputs["tf_htf"].value == "4h"
        assert verdict.trace.inputs["screen1"].value == "BUY"

    def test_both_screens(self, monkeypatch):
        verdict = self._judge(monkeypatch, {"4h": _frame_from_close(_downtrend()),
                                            "30m": _frame_from_close(_neutral_tail(), freq="30min")})

        assert verdict.trace.reason == "screens-not-confirmed"
        first, second = verdict.reason.split("; ")
        assert first == "экран 1 (MACD 4h) не подтверждает покупку: наклон гистограммы вниз"
        assert second.startswith("экран 2 (Stochastic 30m)")

    def test_short_history_is_explained(self, monkeypatch):
        verdict = self._judge(monkeypatch, {"4h": _frame_from_close(_uptrend(n=10)),
                                            "30m": _frame_from_close(_oversold_tail(), freq="30min")})

        assert verdict.reason == "экран 1 (MACD 4h) не подтверждает покупку: мало закрытых свечей (10)"

    def test_incompatible_timeframe(self, monkeypatch):
        verdict = self._judge(monkeypatch, {}, timeframe="1w")

        assert verdict.trace.reason == "timeframe-hierarchy-unavailable"
        assert verdict.reason == "triple_screen: рабочий таймфрейм 1w несовместим с иерархией экранов"

    def test_all_screens_pass(self, monkeypatch):
        verdict = self._judge(monkeypatch, {"4h": _frame_from_close(_uptrend()),
                                            "30m": _frame_from_close(_oversold_tail(), freq="30min")})

        assert not verdict.rejected
        assert verdict.trace.reason == "entry-allowed"
        assert verdict.trace.outcome is TraceOutcome.ACCEPTED
