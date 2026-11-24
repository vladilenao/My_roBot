"""Сессия Финама (add-finam-contour, решения 9–11; tasks 4.3–4.8): правило счёта, снимок, инструменты, допуск."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import grpc
import pytest

from src.broker_contour.admission import AdmissionError
from src.broker_contour.finam.session import FinamSession, adapter
from src.broker_contour.finam.symbols import FinamSymbols, SymbolMismatch
from src.broker_contour.port import (AccountRuleError, BrokerCallError, BrokerSession, InstrumentCard, RobotAccount,
                                     TradingSession)
from tests.unit.broker_contour.finam.fakes import (ACCOUNT_ID, NG, NG_CARD, OTHER_ACCOUNT_ID, SBER, SBER_CARD, SECRET,
                                                   SI, SI_CARD, FakeCatalog, FakeFinam, FakeRpcError, S, T, stubs,
                                                   transport)

ACCOUNT = RobotAccount(ACCOUNT_ID, opened=False, access_level="read_only")


def _session(fake, catalog=None):
    link = transport(fake)
    link.__enter__()
    catalog = catalog or FakeCatalog(SI_CARD, NG_CARD, SBER_CARD)
    return FinamSession(link, FinamSymbols(link, catalog, ACCOUNT_ID), ACCOUNT_ID), link


@pytest.fixture
def opened():
    links = []

    def make(fake, catalog=None):
        session, link = _session(fake, catalog)
        links.append(link)
        return session
    yield make
    for link in links:
        link.__exit__(None, None, None)


# --- правило счёта ------------------------------------------------------------

def test_session_is_read_only_broker_session(opened):
    session = opened(FakeFinam())
    assert isinstance(session, BrokerSession) and not isinstance(session, TradingSession)
    assert session.fee_probes("broker.sqlite3") is None


@pytest.mark.parametrize("readonly, level", [(True, "read_only"), (False, "full_access")])
def test_account_rule_one_account(opened, readonly, level):
    account = opened(FakeFinam(readonly=readonly)).find_account()
    assert (account.account_id, account.access_level, account.masked) == (ACCOUNT_ID, level, "…3322")


def test_account_rule_two_accounts(opened):
    with pytest.raises(AccountRuleError) as exc:
        opened(FakeFinam(account_ids=[ACCOUNT_ID, OTHER_ACCOUNT_ID])).find_account()
    assert "счетов: 2 (…3322, …7766)" in str(exc.value)
    assert ACCOUNT_ID not in str(exc.value) and OTHER_ACCOUNT_ID not in str(exc.value)


def test_account_rule_other_number(opened):
    with pytest.raises(AccountRuleError, match="Токен видит счёт …7766, а FINAM_ACCOUNT_ID задаёт другой"):
        opened(FakeFinam(account_ids=[OTHER_ACCOUNT_ID])).find_account()


# --- снимок -------------------------------------------------------------------

def test_empty_account(opened):
    snapshot = opened(FakeFinam()).read_account(ACCOUNT)
    assert (snapshot.rub, snapshot.liquid_portfolio, snapshot.holdings, snapshot.orders, snapshot.stop_orders) == (
        Decimal(0), None, (), (), ())
    assert snapshot.payload()["capital"] == {"rub": "0.00", "liquid_portfolio": None}


def test_trade_result_second_cash_row_and_closed_position(opened):
    fake = FakeFinam(cash=[("30000", "RUB"), ("-8.21", "RUB"), ("15", "USD")], equity=Decimal("29991.79"),
                     positions=[(NG, "0")])
    snapshot = opened(fake).read_account(ACCOUNT)
    assert snapshot.payload()["capital"] == {"rub": "29991.79", "liquid_portfolio": "29991.79"}
    assert snapshot.holdings == ()


def test_positions_lots_and_names(opened):
    fake = FakeFinam(cash=[("30000", "RUB")], positions=[(SI, "-2"), (SBER, "30")])
    names = {}
    snapshot = opened(fake).read_account(ACCOUNT, names)
    assert snapshot.holdings == (("Si-12.26", -2), ("SBER", 3))       # акция: 30 штук ÷ лот 10
    assert names == {SI_CARD.uid: "Si-12.26", SBER_CARD.uid: "SBER"}  # ключи — только UID ядра


def test_share_not_whole_lots_is_inconsistent(opened):
    with pytest.raises(BrokerCallError) as exc:
        opened(FakeFinam(positions=[(SBER, "15")])).read_account(ACCOUNT)
    assert exc.value.code == "INCONSISTENT"


def test_symbol_without_card(opened):
    names = {}
    snapshot = opened(FakeFinam(positions=[("GAZP@MISX", "10")])).read_account(ACCOUNT, names)
    assert snapshot.holdings == (("инструмент не указан", 10),) and names == {}


def test_active_orders_and_stops(opened):
    fake = FakeFinam(orders=[
        (SI, T.ORDER_TYPE_LIMIT, S.ORDER_STATUS_NEW),
        (SI, T.ORDER_TYPE_LIMIT, S.ORDER_STATUS_PARTIALLY_FILLED),
        (NG, T.ORDER_TYPE_LIMIT, S.ORDER_STATUS_CANCELED),
        (NG, T.ORDER_TYPE_MARKET, S.ORDER_STATUS_FILLED),
        (SI, T.ORDER_TYPE_STOP, S.ORDER_STATUS_WATCHING),
        (NG, T.ORDER_TYPE_STOP, S.ORDER_STATUS_EXECUTED),
    ])
    snapshot = opened(fake).read_account(ACCOUNT)
    assert snapshot.orders == ("Si-12.26", "Si-12.26") and snapshot.stop_orders == ("Si-12.26",)


def test_snapshot_reads_only(opened):
    fake = FakeFinam(positions=[(SI, "1")], orders=[(SI, T.ORDER_TYPE_STOP, S.ORDER_STATUS_WATCHING)])
    opened(fake).read_account(ACCOUNT)
    assert not {"PlaceOrder", "CancelOrder", "PlaceSLTPOrder"} & set(fake.names())


# --- инструменты --------------------------------------------------------------

def test_symbol_for_future_on_rtsx_and_share_on_misx(opened):
    symbols = opened(FakeFinam()).symbols
    assert symbols.symbol_for(SI_CARD) == SI           # не FORTS
    assert symbols.symbol_for(SBER_CARD) == SBER       # не архивный
    assert symbols.card_for(SI) == SI_CARD and symbols.card_for("SIZ6@FORTS") is None


def test_step_mismatch(opened):
    card = InstrumentCard("uid-ng", "NGZ6", "future", "NG-12.26", 1, Decimal("0.0001"))
    with pytest.raises(SymbolMismatch) as exc:
        opened(FakeFinam(), FakeCatalog(card)).symbols.symbol_for(card)
    assert (exc.value.short_name, exc.value.field) == ("NG-12.26", "шаг")


def test_lot_mismatch_uses_trade_lot_size(opened):
    card = InstrumentCard("uid-si", "SiZ6", "future", "Si-12.26", 2, Decimal("1"))
    with pytest.raises(SymbolMismatch) as exc:
        opened(FakeFinam(), FakeCatalog(card)).symbols.symbol_for(card)
    assert exc.value.field == "лот" and exc.value.theirs == 1      # trade_lot_size, не lot_size 1000


def test_mismatch_does_not_hide_position(opened):
    card = InstrumentCard("uid-si", "SiZ6", "future", "Si-12.26", 1, Decimal("5"))
    snapshot = opened(FakeFinam(positions=[(SI, "1")]), FakeCatalog(card)).read_account(ACCOUNT)
    assert snapshot.holdings == (("Si-12.26", 1),)


def test_margin_is_read_every_time(opened):
    fake = FakeFinam(margins=[("7390", "7390"), ("8636.96", "8636.96")])
    symbols = opened(fake).symbols
    assert symbols.margin(NG)["go_buy"] == Decimal("7390")
    assert symbols.margin(NG) == {"go_buy": Decimal("8636.96"), "go_sell": Decimal("8636.96")}
    assert fake.names().count("GetAssetParams") == 2


# --- допуск -------------------------------------------------------------------

def test_facts_common_part_only(opened):
    facts = opened(FakeFinam()).read_facts(ACCOUNT)
    assert facts == {"access_level": "read_only", "warnings": [],
                     "limits": {"GetAccount": 200, "GetOrders": 200, "PlaceOrder": 200, "CancelOrder": 200}}


def test_facts_without_quotas_warn(opened):
    fake = FakeFinam()
    fake.failures["GetUsageMetrics"] = [FakeRpcError(grpc.StatusCode.INTERNAL, "")]
    facts = opened(fake).read_facts(ACCOUNT)
    assert facts["limits"] is None and facts["warnings"] == ["квоты API не прочитаны (GetUsageMetrics: INTERNAL)"]


def test_orders_need_full_access(opened):
    session = opened(FakeFinam())
    session._orders = True
    with pytest.raises(AdmissionError) as exc:
        session.read_facts(ACCOUNT)
    assert exc.value.alarm and "только на чтение" in exc.value.reason


# --- сборка -------------------------------------------------------------------

def test_adapter_opens_catalog_and_transport():
    fake = FakeFinam()
    opened_catalogs = []

    from contextlib import contextmanager

    @contextmanager
    def catalog_factory():
        opened_catalogs.append("open")
        yield FakeCatalog(SI_CARD)
        opened_catalogs.append("closed")

    from src.broker_contour.finam.transport import FinamTransport

    def transport_factory(secret, account_id):
        return FinamTransport(secret, account_id=account_id, channel_factory=lambda target: None,
                              stub_factory=stubs(fake), sleep=lambda seconds: None)

    factory = adapter(secret=SECRET, account_id=ACCOUNT_ID, catalog_factory=catalog_factory,
                      transport_factory=transport_factory)
    with factory() as session:
        assert session.find_account().account_id == ACCOUNT_ID
    assert opened_catalogs == ["open", "closed"]


def test_adapter_without_core_token_is_broker_error():
    from contextlib import contextmanager

    @contextmanager
    def catalog_factory():
        raise ValueError("Не задан TINKOFF_TOKEN")
        yield

    with pytest.raises(BrokerCallError) as exc:
        with adapter(secret=SECRET, account_id=ACCOUNT_ID, catalog_factory=catalog_factory)():
            pass
    assert exc.value.code == "NO_CATALOG"


# --- граница адаптера ---------------------------------------------------------

ADAPTER = Path(__file__).resolve().parents[4] / "src" / "broker_contour" / "finam"
FORBIDDEN = ("t_tech", "src.sandbox", "src.account", "src.broker_contour.tinvest", "src.finam.gateway",
             "src.finam.instruments", "src.finam.records", "src.finam.checks", "src.finam.orders", "src.finam.account")


@pytest.mark.parametrize("path", sorted(ADAPTER.glob("*.py")), ids=lambda path: path.stem)
def test_adapter_knows_no_tinvest_and_no_probe(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    bad = sorted(name for name in found for prefix in FORBIDDEN if name == prefix or name.startswith(prefix + "."))
    probe = sorted(name for name in found if name.startswith("src.finam") and not name.startswith("src.finam.proto"))
    assert bad == [] and probe == [], f"{path.name}: {bad + probe}"
