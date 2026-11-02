"""Имя инструмента, добавленного в контур на ходу, в событиях исполнения (fix-contour-added-instrument-names).

02.11.2026, тестовый робот: эмуляция стартовала с урезанным составом, BR-12.26 и ED-12.26
добавлены через API. Карточки сигналов шли с именем, а все карточки после исполнения —
«контракт не указан»: словарь имён ``publish_execution`` собирался один раз при сборке.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.assembly import contour
from src.events.types import EventType
from src.instruments.model import Instrument

SBER = Instrument("SBER", "SBER", "share", "SBER")
BR = Instrument("BR (Нефть Brent) — BR-12.26", "BRZ6", "future", "BR-12.26")


def _runtime(tmp_path):
    bus, managers = MagicMock(), []

    def manager(*args, **kwargs):
        managers.append(kwargs)
        return MagicMock()

    with patch("src.assembly.contour.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=MagicMock()
    ), patch("src.broker.create_addressable_journal_broker", return_value=MagicMock()), patch(
        "src.trade_management.manager.TradeManager", side_effect=manager
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch(
        "src.assembly.contour.print_contract_metadata"
    ), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([SBER], MagicMock(), bus, tmp_path)
    return runtime, bus, managers[0]["execution_observer"]


def _fill(observer, instrument_id):
    execution = SimpleNamespace(status="fill", trade_id=f"future-br-macd:{instrument_id}:5m:SELL",
                                timestamp=datetime(2026, 11, 2, 8, 21, tzinfo=timezone.utc), filled_quantity=4,
                                price=Decimal("110.19"), reason="limit-fill", fee=Decimal(6), fee_source="configured",
                                execution_id="exec-1")
    observer(execution, {"action_type": "OPEN", "instrument_id": instrument_id, "side": "SELL"})


@pytest.mark.parametrize("contracts", [{"BRZ6": object()}, None], ids=["with-card", "without-card"])
def test_instrument_added_on_the_fly_named_in_execution_events(tmp_path, contracts):
    runtime, bus, observer = _runtime(tmp_path)
    runtime.add_instrument(BR, contracts)

    _fill(observer, "BRZ6")

    events = [call.args[0] for call in bus.publish.call_args_list if call.args[0].type is EventType.TRADE_OPENED]
    assert [event.instrument for event in events] == ["BR-12.26"]
