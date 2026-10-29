"""Классификация разрывов для графика не влияет на торговый шаг."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from src.bot.trading_bot import TradingBot

FRAME = pd.DataFrame({"datetime": pd.date_range("2026-10-13 10:00", periods=100, freq="15min")})


def _bot(cache):
    bot = object.__new__(TradingBot)
    bot._data_cache = cache
    return bot


def test_classification_receives_window_tail_and_is_passed_through():
    seen = []
    breaks = {("a", "b"): {"kind": "closed", "missing": 1}}
    bot = _bot(SimpleNamespace(chart_breaks=lambda instrument, tf, frame: seen.append((tf, len(frame))) or breaks))
    assert bot._chart_breaks("SBER", "15m", FRAME) is breaks
    assert seen == [("15m", 81)]


def test_classification_failure_or_foreign_result_is_ignored():
    def broken(*args):
        raise RuntimeError("store")
    assert _bot(SimpleNamespace(chart_breaks=broken))._chart_breaks("SBER", "15m", FRAME) is None
    assert _bot(Mock())._chart_breaks("SBER", "15m", FRAME) is None
    assert _bot(SimpleNamespace())._chart_breaks("SBER", "15m", FRAME) is None
    assert _bot(Mock())._chart_breaks("SBER", "15m", FRAME.iloc[:0]) is None
