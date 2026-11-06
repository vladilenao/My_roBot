"""Сторож исполнения 1m и цены позиций после рестарта (fix-sandbox-restart-safety).

05.11 (песочница, ED-12.26): барьер покрытия 8 ч 15 мин держал минутки ED, предупреждение было
одно, в 15:03. Пока исполнение стояло, виртуальный стоп ED не проверялся, а переоценка без цены
ED запрещала входы всем инструментам.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from src.assembly import contour
from src.assembly.contour import BrokerTrading, _ExecutionWatch
from src.instruments.model import Instrument

T0 = datetime(2026, 11, 5, 12, 0, 18, tzinfo=timezone.utc)
EXECUTED = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
RECEIVED = datetime(2026, 11, 5, 12, 2, tzinfo=timezone.utc)
ED = Instrument("ED", "EDZ6", "future", "ED-12.26")


class Report:
    def __init__(self):
        self.calls = []

    def __call__(self, raised, resumed, holds):
        self.calls.append(([item["name"] for item in raised], [item["name"] for item in resumed],
                           [item["name"] for item in holds]))


def held(name="ED-12.26"):
    return {"EDZ6": (name, EXECUTED, RECEIVED)}


def test_short_hold_is_quiet_long_hold_alarms_once_and_resumes(caplog):
    report = Report()
    watch = _ExecutionWatch(report=report)
    caplog.set_level(logging.INFO, logger="src.assembly.contour")

    watch.observe(T0, held(), lambda key: True)
    watch.observe(T0 + timedelta(minutes=4), held(), lambda key: True)
    assert report.calls == []

    watch.observe(T0 + timedelta(minutes=5), held(), lambda key: True)
    watch.observe(T0 + timedelta(minutes=6), held(), lambda key: True)
    assert report.calls == [(["ED-12.26"], [], ["ED-12.26"])]
    assert watch.alarms(T0 + timedelta(minutes=6))[0]["minutes"] == 6

    watch.observe(T0 + timedelta(minutes=21), held(), lambda key: True)
    warnings = [record for record in caplog.records if "стоит" in record.getMessage()]
    assert len(warnings) == 2                       # тревога и повтор через 15 мин, не каждый тик
    assert len(report.calls) == 1

    watch.observe(T0 + timedelta(minutes=22), {}, lambda key: True)
    assert report.calls[-1] == ([], ["ED-12.26"], [])
    assert any("возобновлено после 22 мин" in record.getMessage() for record in caplog.records)


def test_hold_without_trades_does_not_alarm():
    report = Report()
    watch = _ExecutionWatch(report=report)

    for minute in range(0, 30, 5):
        watch.observe(T0 + timedelta(minutes=minute), held(), lambda key: False)
    watch.observe(T0 + timedelta(minutes=31), {}, lambda key: False)

    assert report.calls == []


def _runtime(tmp_path, cache, manager, alarm):
    storage = MagicMock()
    storage.load_trades.return_value = ()
    storage.execution_bar_boundaries.side_effect = lambda: {"EDZ6": (EXECUTED, True)}
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    trading = BrokerTrading(broker_factory=lambda storage: broker, capital=lambda: Decimal(1),
                            initial_deposit=Decimal(1), execution_alarm=alarm)
    with patch("src.trade_journal.storage.Storage", return_value=storage), patch(
        "src.trade_management.manager.TradeManager", return_value=manager
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch(
        "src.assembly.contour.print_contract_metadata"
    ), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([ED], cache, MagicMock(), tmp_path, broker_trading=trading)
    return runtime, broker


def test_held_execution_with_open_trade_reaches_owner_and_seeds_price(tmp_path):
    frame = pd.DataFrame({"datetime": [EXECUTED - timedelta(minutes=1), RECEIVED],
                          "open": 1.12, "high": 1.122, "low": 1.119, "close": [1.1216, 1.1219], "volume": 5})
    cache = SimpleNamespace(frame_for=lambda instrument, timeframe: frame,
                            contiguous_after=lambda instrument, timeframe, fresh, last: fresh.iloc[0:0])
    clock = [T0]
    manager = MagicMock()
    manager._clock.now.side_effect = lambda: clock[0]
    report = Report()
    runtime, broker = _runtime(tmp_path, cache, manager, report)

    runtime.post_tick({"1m"})
    manager.mark_to_market.assert_called_once_with({"EDZ6": 1.1216})   # исполненная 11:59, не удержанная 12:02
    clock[0] = T0 + timedelta(minutes=5)
    runtime.post_tick({"1m"})

    broker.track_bar.assert_not_called()
    assert report.calls == [(["ED-12.26"], [], ["ED-12.26"])]
