from datetime import datetime
from unittest.mock import patch

import pytest

from src.api.emulator_client import HistoricalClient
from src.instruments.futures import FuturesCard, catalog_digest, family_for, lifecycle_time


def row(**overrides):
    return {"instrument_id": "ng-1", "ticker": "NGV6", "name": "NG-10.26 Природный газ",
            "basic_asset": "Газ (США)", "basic_asset_size": 100,
            "first_trade_date": "2026-03-19T10:00:00+03:00",
            "last_trade_date": "2026-10-28T11:00:00Z",
            "expiration_date": "2026-10-29T00:00:00Z", "min_price_increment": .001,
            "min_price_increment_amount": 8.49, "initial_margin_on_buy": 7683,
            "initial_margin_on_sell": 7777, **overrides}


@pytest.mark.parametrize("ticker,name,family", [
    ("RIU6", "RTS-9.26", "RTS"), ("BRV6", "BR-10.26", "BR"),
    ("SiU6", "Si-9.26", "SI"), ("SRU6", "SBRF-9.26", "SBRF"),
    ("EDU6", "ED-9.26", "ED"), ("NGV6", "NG-10.26", "NG"),
    ("GDU6", "GOLD-9.26", "GOLD"), ("SVU6", "SILV-9.26", "SILV"),
])
def test_family_aliases(ticker, name, family):
    assert family_for(ticker, name) == family


def test_real_card_and_timezone():
    card = FuturesCard.from_row(row())
    assert card.family == "NG"
    assert card.first_trade_date == datetime(2026, 3, 19, 7)
    assert card.problems() == ()
    assert card.instrument.short_name == "NG-10.26"


@pytest.mark.parametrize("date", ["2026-03-19", "2026-03-19T07:00:00", "bad"])
def test_incomplete_dates_are_not_invented(date):
    card = FuturesCard.from_row(row(first_trade_date=date))
    assert card.first_trade_date is None
    assert any("first_trade_date" in problem for problem in card.problems())


def test_missing_date_and_mini():
    card = FuturesCard.from_row(row(first_trade_date=None, name="NG-10.26 мини"))
    assert not card.standard
    assert "отсутствует first_trade_date" in card.problems()
    assert lifecycle_time(None) is None


def test_ambiguous_family():
    card = FuturesCard.from_row(row(basic_asset="BR"))
    assert card.family is None
    assert "неоднозначное семейство карточки" in card.problems()


def test_catalog_is_read_without_broker_token_and_carries_sdk_fields():
    with patch.object(HistoricalClient, "_get", return_value=[row()]) as get:
        client = HistoricalClient("http://local")
        cards = client.futures_catalog()
        future = client.instruments.futures().instruments[0]
    assert cards[0].uid == "ng-1"
    assert future.first_trade_date.hour == 10
    assert future.last_trade_date is not None
    assert future.basic_asset == "Газ (США)"
    assert get.call_args_list[0].args[1]["instrument_type"] == "future"


def test_digest_does_not_depend_on_input_order():
    a, b = FuturesCard.from_row(row()), FuturesCard.from_row(row(instrument_id="ng-2"))
    assert catalog_digest([a, b]) == catalog_digest([b, a])


def test_same_underlying_is_not_same_contract_series():
    miniature = FuturesCard.from_row(row(ticker="MNGV6", name="MNG-10.26", basic_asset="NG"))
    foreign = FuturesCard.from_row(row(class_code="CME"))
    assert not miniature.standard and not foreign.standard
