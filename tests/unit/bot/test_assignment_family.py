"""Привязки фьючерса по семейству контракта (fix-futures-family-assignments)."""

from __future__ import annotations

import pytest

from src.bot.trading_bot import assignments_for
from src.history.preflight import instrument_assignments
from src.instruments import Instrument
from src.instruments.futures import assignment_family
from src.strategies.contracts import Assignment


def _binding(binding_id):
    return [Assignment(binding_id, "macd_rsi_stoch", "levels_rr", timeframe="15m")]


SHARES = {"SBER": _binding("share-sber")}
FUTURES = {key: _binding(f"future-{key.lower()}") for key in ("NG", "BR", "SI", "RTS", "GOLD", "SILV")}
# SBRF записан кодом семейства — так тоже допустимо.
FUTURES["SR"] = _binding("future-sr")

CASES = [
    # Карточки состава контуров: семейства нет — по тикеру и короткому имени.
    (Instrument("RTS-12.26", "RIZ6", "future", short_name="RTS-12.26"), ["future-rts"]),
    (Instrument("SBRF-12.26", "SRZ6", "future", short_name="SBRF-12.26"), ["future-sr"]),
    (Instrument("GOLD-12.26", "GDZ6", "future", short_name="GOLD-12.26"), ["future-gold"]),
    (Instrument("SILV-12.26", "SVZ6", "future", short_name="SILV-12.26"), ["future-silv"]),
    (Instrument("Si-12.26", "SiZ6", "future", short_name="Si-12.26"), ["future-si"]),
    (Instrument("BR-12.26", "BRZ6", "future"), ["future-br"]),
    # Семейство из карточки — как раньше.
    (Instrument("NG-12.26", "NGZ6", "future", family="NG"), ["future-ng"]),
    (Instrument("SBER", "SBER", "share"), ["share-sber"]),
    (Instrument("ED-12.26", "EDZ6", "future", short_name="ED-12.26"), []),
]


@pytest.mark.parametrize("instrument, ids", CASES)
def test_trading_finds_bindings_by_contract_family(instrument, ids):
    assert [item.id for item in assignments_for(instrument, SHARES, FUTURES)] == ids


@pytest.mark.parametrize("instrument, ids", CASES)
def test_history_preflight_finds_same_bindings(instrument, ids):
    assert [item.id for item in instrument_assignments(instrument, SHARES, FUTURES)] == ids


def test_assignment_family():
    assert assignment_family("RIZ6", "RTS-12.26") == "RTS"
    assert assignment_family("RIZ6") == "RTS"
    assert assignment_family("NGZ6", None, "NG") == "NG"
    assert assignment_family("XXZ6", "XX-12.26") is None
    # Тикер одного семейства, имя другого — неоднозначно, без догадки.
    assert assignment_family("RIZ6", "NG-12.26") is None
