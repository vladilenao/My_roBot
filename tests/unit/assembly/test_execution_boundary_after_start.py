"""Граница исполнения 1m берётся на первом баре, а не при сборке контура (add-tinvest-orders 3.10).

03.11 (Si): процесс песочницы между сборкой и первым тиком провёл вход, исполненный за простой.
Граница, прочитанная при сборке, указывала на прошлую сделку Si (8 часов назад), и контур
прогнал старые минуты против стопа новой сделки.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from src.assembly import contour
from src.instruments.model import Instrument

SBER = Instrument("SBER", "SBER", "share", "SBER")
T0 = datetime(2026, 11, 3, 5, 32, tzinfo=timezone.utc)


def test_boundary_read_on_first_bar_sees_fill_consumed_after_build(tmp_path):
    frame = pd.DataFrame({"datetime": [T0 + timedelta(minutes=m) for m in range(6)],
                          "open": 300.0, "high": 301.0, "low": 299.0, "close": 300.5, "volume": 10})
    cache = SimpleNamespace(frame_for=lambda instrument, timeframe: frame)
    boundaries = {"SBER": (T0, True)}                       # при сборке: последняя сделка — прошлая
    storage = MagicMock()
    storage.load_trades.return_value = ()
    storage.execution_bar_boundaries.side_effect = lambda: dict(boundaries)
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    with patch("src.assembly.contour.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch(
        "src.assembly.contour.print_contract_metadata"
    ), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([SBER], cache, MagicMock(), tmp_path)

    boundaries["SBER"] = (T0 + timedelta(minutes=4), True)  # вход, исполненный за простой, проведён при старте
    runtime.post_tick({"1m"})

    executed = [call.args[0] for call in broker.track_bar.call_args_list]
    assert executed == [T0 + timedelta(minutes=4), T0 + timedelta(minutes=5)]
