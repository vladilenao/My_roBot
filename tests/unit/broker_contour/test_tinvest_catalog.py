"""Каталог карточек ядра на Т-Инвест (add-finam-contour, решение 4; tasks 3.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.broker_contour.port import BrokerCallError, InstrumentCard, InstrumentCatalog
from src.broker_contour.tinvest.catalog import TInvestCatalog, catalog
from src.broker_contour.tinvest.gateway import ReferenceGateway
from tests.unit.sandbox.fakes import NG_UID, SBER_UID, FakeBroker, FakeClient

TOKEN = "t.fake-core-token-0000"


def _factory(broker):
    return lambda token: ReferenceGateway(token, client_factory=lambda value: FakeClient(broker, value),
                                          sleep=lambda seconds: None)


def _calls(broker) -> list[str]:
    return [call[0] for call in broker.calls]


def test_cards_by_ticker_and_uid():
    broker = FakeBroker()
    with catalog(TOKEN, gateway_factory=_factory(broker))() as found:
        assert isinstance(found, InstrumentCatalog)
        ng = found.by_ticker("NGZ6", "future")
        sber = found.by_uid(SBER_UID)
        assert ng == InstrumentCard(NG_UID, "NGZ6", "future", "NG-12.26", 1, Decimal("0.001"))
        assert sber == InstrumentCard(SBER_UID, "SBER", "share", "SBER", 10, Decimal("0.01"))
        assert found.by_uid(NG_UID) is ng                 # кэш: по тикеру найдено — по UID без вызова
        assert found.by_ticker("UNKNOWN", "share") is None


def test_cache_saves_calls():
    broker = FakeBroker()
    with catalog(TOKEN, gateway_factory=_factory(broker))() as found:
        found.by_ticker("SBER", "share")
        found.by_ticker("sber", "share")
        found.by_uid(SBER_UID)
    assert _calls(broker).count("instruments.get_instrument_by") == 1


def test_reference_gateway_is_instruments_only():
    broker = FakeBroker()
    with _factory(broker)(TOKEN) as gateway, pytest.raises(BrokerCallError) as exc:
        gateway.call("operations.get_portfolio", account_id="x")
    assert exc.value.code == "REFERENCE_ONLY"
    assert "operations.get_portfolio" not in _calls(broker)


def test_catalog_card_name_rule_matches_snapshot_rule():
    """Короткое имя каталога — то же правило, что у снимка Т-Инвест (без третьей копии)."""
    from src.sandbox.instruments import short_name_by_uid

    broker = FakeBroker()
    with _factory(broker)(TOKEN) as gateway:
        assert TInvestCatalog(gateway).by_uid(NG_UID).short_name == short_name_by_uid(gateway, NG_UID, {})
