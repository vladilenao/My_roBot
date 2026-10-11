from datetime import datetime
import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.history.selection import HistorySelection
from src.instruments.model import Instrument
from src.instruments.selector import select_instruments
from src.instruments.futures import FuturesCard


def cards():
    return tuple(FuturesCard.from_row({
        "instrument_id": f"uid-{m}", "ticker": f"NG-{m}.26", "name": f"NG-{m}.26 Газ",
        "first_trade_date": "2026-01-01T00:00:00Z", "expiration_date": f"2026-{m:02d}-25T00:00:00Z",
        "min_price_increment": 1, "min_price_increment_amount": 1,
        "initial_margin_on_buy": 10, "initial_margin_on_sell": 10,
    }) for m in (7, 8, 9))


def test_shift_keeps_catalog_and_changes_segments(tmp_path):
    selected = HistorySelection(families=("NG",), catalog=cards())
    a = selected.planned(datetime(2026, 7, 1), datetime(2026, 9, 1), {}, 2)
    b = a.planned(datetime(2026, 8, 1), datetime(2026, 9, 1), {}, 2)
    assert a.catalog is b.catalog
    assert len(a.chains[0]) == 3 and len(b.chains[0]) == 2
    b.save(tmp_path)
    saved = json.loads((tmp_path / "contract_chain.json").read_text())
    assert saved["catalog_digest"] == a.payload()["catalog_digest"]
    assert saved["segments"][0]["contract"] == "NG-8.26"


def test_conflicting_auto_and_fixed_family():
    selected = HistorySelection((Instrument("NG", "NG-7.26", "future", "NG-7.26"),), ("NG",), cards())
    with pytest.raises(ValueError, match="одновременно"):
        selected.planned(datetime(2026, 7, 1), datetime(2026, 9, 1), {}, 2)


def test_history_default_family_deduplicates_and_reads_once():
    provider = MagicMock()
    client = provider.client_context.return_value.__enter__.return_value = Mock()
    client.futures_catalog.return_value = cards()
    with patch("builtins.input", side_effect=["2", "", "NG,NG", "да", "2", "1", "NG", "нет"]):
        selection = select_instruments(client_provider=provider, historical=True)
    assert selection.families == ("NG",)
    client.futures_catalog.assert_called_once()


def test_new_run_reads_new_catalog():
    provider = MagicMock()
    client = provider.client_context.return_value.__enter__.return_value
    a = cards()
    b = a[1:]
    client.futures_catalog.side_effect = [a, b]
    with patch("builtins.input", side_effect=["2", "", "NG", "нет", "2", "", "NG", "нет"]):
        first = select_instruments(client_provider=provider, historical=True)
        second = select_instruments(client_provider=provider, historical=True)
    assert first.catalog == a and second.catalog == b
    assert first.payload()["catalog_digest"] != second.payload()["catalog_digest"]
