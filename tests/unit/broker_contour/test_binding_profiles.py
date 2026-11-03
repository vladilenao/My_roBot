"""Привязки участника эмуляции и их профили (add-binding-profiles-api)."""

from __future__ import annotations

import pytest

from src.broker_contour.bindings import binding_brief, binding_catalog, instrument_bindings
from src.strategies.contracts import Assignment

SHARES = {"SBER": [Assignment("share-sber-ma", "ma_cloud_rsi_macd", "ma_cloud", filter_profile="raw",
                              timeframe="1h")]}
FUTURES = {
    "BR": [Assignment("future-br-flat", "flat_triangle", "levels_rr", filter_profile="triple_screen", timeframe="1h"),
           Assignment("future-br-ma", "ma_cloud_rsi_macd", "ma_cloud", timeframe="15m")],
    "SR": [Assignment("future-sr-harmonic", "harmonic_abcd", "pattern_targets", timeframe="1h")],
    "RTS": [Assignment("future-rts-flat", "flat_triangle", "levels_rr", timeframe="1h")],
}
CATALOG = binding_catalog(SHARES, FUTURES)


def _ids(card):
    return [item["id"] for item in instrument_bindings(card, CATALOG)]


@pytest.mark.parametrize("card, ids", [
    ({"ticker": "SBER", "instrument_type": "share"}, ["share-sber-ma"]),
    ({"ticker": "BRZ6", "instrument_type": "future", "family": "BR"}, ["future-br-flat", "future-br-ma"]),
    # Семейство SBRF в конфигурации записано кодом SR — как у торговли.
    ({"ticker": "SRZ6", "instrument_type": "future", "family": "SBRF"}, ["future-sr-harmonic"]),
    # Старая карточка без тикера и семейства — по имени.
    ({"name": "BR-12.26", "instrument_type": "future"}, ["future-br-flat", "future-br-ma"]),
    # Карточки состава контуров — без семейства (fix-futures-family-assignments): по тикеру и имени.
    ({"ticker": "RIZ6", "short_name": "RTS-12.26", "instrument_type": "future", "family": None}, ["future-rts-flat"]),
    ({"ticker": "SRZ6", "short_name": "SBRF-12.26", "instrument_type": "future", "family": None},
     ["future-sr-harmonic"]),
    ({"name": "RTS-12.26", "instrument_type": "future"}, ["future-rts-flat"]),
    ({"ticker": "GAZP", "instrument_type": "share"}, []),
    ({"ticker": "NGZ6", "instrument_type": "future", "family": "NG"}, []),
])
def test_instrument_bindings_follow_trading_rules(card, ids):
    assert _ids(card) == ids


def test_brief_carries_filter_and_management():
    assert binding_brief(CATALOG["future-br-flat"]) == {
        "id": "future-br-flat", "strategy": "flat_triangle", "timeframe": "1h",
        "filter": "triple_screen", "management": "levels_rr"}
    assert binding_brief(CATALOG["share-sber-ma"])["filter"] == "raw"
    assert binding_brief(CATALOG["future-br-ma"])["filter"] == "basic_levels"
