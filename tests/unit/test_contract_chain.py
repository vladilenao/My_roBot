from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from src.history.contract_chain import ContractChainError, build_chain
from src.instruments.futures import FuturesCard


def card(month, **kw):
    return FuturesCard.from_row({
        "instrument_id": f"uid-{month}", "ticker": f"NG-{month}.26", "name": f"NG-{month}.26 Газ",
        "first_trade_date": "2026-01-01T07:00:00Z",
        "expiration_date": f"2026-{month:02d}-25T12:00:00Z",
        "min_price_increment": .01, "min_price_increment_amount": 1,
        "initial_margin_on_buy": 10, "initial_margin_on_sell": 10, **kw,
    })


def test_three_contracts_and_calendar_boundaries():
    cards = [card(7), card(8), card(9)]
    chain = build_chain(cards, "NG", datetime(2026, 7, 1), datetime(2026, 9, 1), 2)
    assert [part.card.short_name for part in chain] == ["NG-7.26", "NG-8.26", "NG-9.26"]
    assert chain[0].end == datetime(2026, 7, 23, 12)
    assert chain[1].end == datetime(2026, 8, 23, 12)
    assert chain[2].end == datetime(2026, 9, 1)
    assert chain == build_chain(cards[::-1], "ng", datetime(2026, 7, 1), datetime(2026, 9, 1), 2)


def test_start_at_cutoff_and_end_at_cutoff():
    cutoff = datetime(2026, 7, 23, 12)
    assert len(build_chain([card(7)], "NG", datetime(2026, 7, 1), cutoff, 2)) == 1
    assert build_chain([card(7), card(8)], "NG", cutoff, cutoff+timedelta(days=1), 2)[0].card == card(8)


def test_earlier_last_trade_and_notice():
    c = card(7, last_trade_date="2026-07-24T15:00:00+03:00", first_notice_date="2026-07-24T10:00:00Z")
    part, = build_chain([c], "NG", datetime(2026, 7, 1), datetime(2026, 7, 2), 2)
    assert part.cutoff == datetime(2026, 7, 22, 10)
    assert part.deadline_source == "first_notice_date"


def test_expiration_only_and_family_horizon():
    a, = build_chain([card(7)], "NG", datetime(2026, 7, 1), datetime(2026, 7, 2), 3)
    assert a.deadline_source == "expiration_only"
    assert a.cutoff == datetime(2026, 7, 22, 12)
    assert build_chain([card(7)], "NG", datetime(2026, 7, 1), datetime(2026, 7, 2), 2)[0].cutoff != a.cutoff


@pytest.mark.parametrize("cards,expected", [
    ([card(7), card(8, first_trade_date="2026-07-24T00:00:00Z"), card(9)], "следующий контракт недопустим"),
    ([card(7), replace(card(7), uid="other", ticker="other")], "одинаковой экспирацией"),
    ([card(7, first_trade_date="2026-07-24T00:00:00Z")], "first_trade_date не раньше cutoff"),
    ([card(7)], "отсутствует следующий"),
    ([card(7), card(8, min_price_increment_amount=None), card(9)], "отсутствует step_cost"),
])
def test_invalid_chain_does_not_skip_contract(cards, expected):
    with pytest.raises(ContractChainError, match=expected):
        build_chain(cards, "NG", datetime(2026, 7, 1), datetime(2026, 9, 1), 2)


def test_not_yet_issued_and_missing_dates():
    with pytest.raises(ContractChainError, match="нет допустимого"):
        build_chain([card(7)], "NG", datetime(2025, 12, 1), datetime(2026, 7, 1), 2)
    with pytest.raises(ContractChainError, match="first_trade_date"):
        build_chain([card(7, first_trade_date=None)], "NG", datetime(2026, 7, 1), datetime(2026, 7, 2), 2)


def test_long_range_has_no_183_day_filter():
    chain = build_chain([card(m) for m in range(2, 13)], "NG", datetime(2026, 2, 1), datetime(2026, 12, 1), 2)
    assert len(chain) == 11
    assert chain[-1].card.short_name == "NG-12.26"


def test_unissued_immediate_next_is_not_filtered_out_by_range_end():
    with pytest.raises(ContractChainError, match="следующий контракт недопустим"):
        build_chain([card(7), card(8, first_trade_date="2026-08-01T00:00:00Z"), card(9)],
                    "NG", datetime(2026, 7, 1), datetime(2026, 7, 24), 2)


def test_ambiguous_card_cannot_disappear_from_chain():
    with pytest.raises(ContractChainError, match="неоднозначное семейство"):
        build_chain([card(7), card(8, basic_asset="BR"), card(9)],
                    "NG", datetime(2026, 7, 1), datetime(2026, 9, 1), 2)


def test_ticker_reuse_is_not_contract_identity_collision():
    first, second = card(7), replace(card(8), ticker=card(7).ticker)
    chain = build_chain([first, second], "NG", datetime(2026, 7, 1), datetime(2026, 8, 1), 2)
    assert len(chain) == 2
    assert first.instrument.contract_id != second.instrument.contract_id
