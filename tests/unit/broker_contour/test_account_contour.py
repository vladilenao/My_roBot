"""Контур «Счёт» (add-account-contour): шлюз, правило счёта, допуск, первый старт, разведка комиссий."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from functools import partial

import pytest
from t_tech.invest import schemas as s

from src.broker_contour.tinvest.account import account_by_id
from src.broker_contour.tinvest.admission import NO_DERIVATIVES, read_facts
from src.account.fee_probes import FeeProbes, read_fee_probes
from src.broker_contour.port import AccountSnapshot
from src.broker_contour.admission import AdmissionError, admit_account, check_settings
from src.broker_contour.process import BrokerContour, BrokerContourSettings
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.events.bus import EventBus
from src.broker_contour.tinvest.session import TInvestSession, adapter
from src.sandbox.gateway import SANDBOX_ONLY, AccountGateway, BrokerCallError
from tests.unit.broker_contour.test_broker_contour import TRADING_CALLS as WRITE_METHODS, Collector, Stop
from tests.unit.sandbox.fakes import NG_UID, NOW, FakeBroker, FakeClient, money

ACCOUNT_ID = "2000123456-real-7f21"
TOKEN = "fake-trade-token-0123456789"
FULL = s.AccessLevel.ACCOUNT_ACCESS_LEVEL_FULL_ACCESS
READ = s.AccessLevel.ACCOUNT_ACCESS_LEVEL_READ_ONLY


def _account(account_id=ACCOUNT_ID, level=READ, status=s.AccountStatus.ACCOUNT_STATUS_OPEN):
    return s.Account(id=account_id, name="Брокерский счёт", status=status, access_level=level)


def _gateway(broker):
    return lambda token: AccountGateway(token, client_factory=lambda value: FakeClient(broker, value),
                                        sleep=lambda seconds: None)


def _find(gateway):
    return account_by_id(gateway, ACCOUNT_ID)


def _sessions(broker, *, orders):
    """Сессии Т-Инвест «Счёта» на фейковом брокере (refactor-broker-port)."""
    return adapter("account", token=TOKEN, account_id=ACCOUNT_ID, orders=orders, gateway_factory=_gateway(broker),
                   rule=_find, facts=partial(read_facts, orders=orders))


# --- шлюз ---------------------------------------------------------------------

def test_account_gateway_refuses_sandbox_calls_before_network():
    broker = FakeBroker(accounts=[_account()])
    with _gateway(broker)(TOKEN) as gateway, pytest.raises(BrokerCallError) as exc:
        gateway.call("sandbox.sandbox_pay_in", account_id=ACCOUNT_ID, amount=money(1))
    assert exc.value.code == SANDBOX_ONLY and broker.calls == []


def test_account_gateway_requires_token():
    with pytest.raises(ValueError, match="TINKOFF_TRADE_TOKEN"):
        AccountGateway("")


# --- правило счёта ---------------------------------------------------------------

def test_one_open_account_with_read_only_access():
    broker = FakeBroker(accounts=[_account(), _account("2000999-closed-0000", status=s.AccountStatus.ACCOUNT_STATUS_CLOSED)])
    with _gateway(broker)(TOKEN) as gateway:
        account = _find(gateway)
    assert account.account_id == ACCOUNT_ID and account.access_level == "read_only" and not account.opened
    assert account.masked == "…7f21"


@pytest.mark.parametrize("accounts, text", [
    ([_account(), _account("2000555-other-0002")], "Токен видит открытых счетов: 2 (…7f21, …0002)"),
    ([], "Токен видит открытых счетов: 0"),
    ([_account("2000555-other-0002")], "Токен видит счёт …0002, а TINKOFF_ACCOUNT_ID задаёт другой"),
    ([_account(level=s.AccessLevel.ACCOUNT_ACCESS_LEVEL_NO_ACCESS)], "нет доступа к счёту …7f21"),
])
def test_account_rule_refusals(tmp_path, accounts, text):
    broker = FakeBroker(accounts=accounts)
    with _gateway(broker)(TOKEN) as gateway, pytest.raises(AdmissionError) as exc:
        admit_account(TInvestSession(gateway, rule=_find), BrokerStore(tmp_path / BROKER_DATABASE_FILE), broker="account")
    assert text in exc.value.reason and exc.value.alarm
    assert ACCOUNT_ID not in exc.value.reason and "2000555-other-0002" not in exc.value.reason


def test_account_id_env_checked_without_network():
    with pytest.raises(AdmissionError, match="Не задан TINKOFF_ACCOUNT_ID"):
        check_settings(token=TOKEN, token_env="TINKOFF_TRADE_TOKEN", bot_token="b", bot_env="B", channel_id="c",
                       channel_env="C", account_id=None, account_id_env="TINKOFF_ACCOUNT_ID")


# --- права токена и допуски ------------------------------------------------------

def _facts(broker, *, orders):
    with _gateway(broker)(TOKEN) as gateway:
        return read_facts(gateway, _find(gateway), orders=orders)


def test_facts_read_only_on_r1():
    facts = _facts(FakeBroker(accounts=[_account()]), orders=False)
    assert facts == {"access_level": "read_only", "qualified_for_work_with": ["bond", "derivative"],
                     "derivatives": True, "tariff": "trader", "limits": {"orders": 100}, "warnings": []}


def test_no_derivatives_is_warning_on_r1_and_refusal_on_r2():
    broker = FakeBroker(accounts=[_account(level=FULL)], qualified=["bond"])
    facts = _facts(broker, orders=False)
    assert not facts["derivatives"] and facts["warnings"] == [NO_DERIVATIVES]
    with pytest.raises(AdmissionError, match="Нет допуска к срочным контрактам"):
        _facts(broker, orders=True)


def test_read_only_token_refused_with_orders():
    with pytest.raises(AdmissionError, match="нужен полный доступ"):
        _facts(FakeBroker(accounts=[_account()]), orders=True)


def test_limits_of_contour_methods_only():
    from src.broker_contour.tinvest.admission import _limits

    prefix = "tinkoff.public.invest.api.contract.v1."
    response = s.GetUserTariffResponse(unary_limits=[
        s.UnaryLimit(limit_per_minute=5, methods=[prefix + "OperationsService/GetBrokerReport"]),
        s.UnaryLimit(limit_per_minute=200, methods=[prefix + "OperationsService/GetOperationsByCursor",
                                                    prefix + "OperationsService/GetPositions"]),
        s.UnaryLimit(limit_per_minute=50, methods=[prefix + "StopOrdersService/PostStopOrder"])])
    assert _limits(response) == {"operations": 200, "stop_orders": 50}


def test_tariff_failure_is_warning():
    facts = _facts(FakeBroker(accounts=[_account()], tariff_fails=True), orders=False)
    assert facts["limits"] is None and facts["warnings"][0].startswith("лимиты API не прочитаны")


def test_fingerprint_of_other_account_refused(tmp_path):
    from src.broker_contour.fingerprint import fingerprint_of

    store = BrokerStore(tmp_path / BROKER_DATABASE_FILE)
    store.save_fingerprint(fingerprint_of("account", "2000123456-real-4a1b"))
    with _gateway(FakeBroker(accounts=[_account()]))(TOKEN) as gateway, pytest.raises(AdmissionError) as exc:
        admit_account(TInvestSession(gateway, rule=_find, facts=partial(read_facts, orders=False)), store,
                      broker="account")
    assert "другому счёту …4a1b" in exc.value.reason


# --- процесс ---------------------------------------------------------------------

def _settings(tmp_path):
    return BrokerContourSettings(
        contour_id="account", broker="account", directory=tmp_path / "contours" / "account",
        core_db=tmp_path / "core.sqlite3", version="9.9.9", token=TOKEN, token_env="TINKOFF_TRADE_TOKEN",
        account_id=ACCOUNT_ID, account_id_env="TINKOFF_ACCOUNT_ID", bot_token="123456:fake-bot-token-abcdef",
        bot_env="TELEGRAM_ACCOUNT_BOT_TOKEN", channel_id="-100700", channel_env="TELEGRAM_ACCOUNT_CHANNEL_ID",
        reconcile_every_secs=0, step_secs=0.02)


def _run(tmp_path, broker, steps=3):
    collector, bus, stop = Collector(), EventBus(), Stop()
    bus.subscribe_all([collector])
    contour = BrokerContour(_settings(tmp_path), session_factory=_sessions(broker, orders=False), bus=bus, stop=stop,
                            pid=4242)
    thread = threading.Thread(target=contour.run)
    thread.start()
    for _ in range(500):
        if len([m for m, _ in broker.calls if m == "operations.get_positions"]) >= steps:
            break
        threading.Event().wait(0.01)
    stop.request("тест")
    thread.join(5)
    status = json.loads((tmp_path / "contours" / "account" / "status.json").read_text(encoding="utf-8"))
    return collector, status


def test_read_only_contour_on_empty_account(tmp_path):
    broker = FakeBroker(accounts=[_account()], rub=Decimal("150000"))
    collector, status = _run(tmp_path, broker)
    started = next(event for event in collector.events if event.get("kind") == "started")
    assert started.get("contour") == "account" and started.get("access_level") == "read_only"
    assert list(started.get("warnings")) == [] and started.get("account") == "…7f21"
    assert started.get("orders_enabled") is False
    assert status["admission"]["access_level"] == "read_only" and status["orders_enabled"] is False
    assert status["capital"]["rub"] == "150000.00" and status["account"] == "…7f21"
    assert not {method for method, _ in broker.calls} & WRITE_METHODS
    assert "reconcile_alarm" not in collector.kinds()


def test_first_start_on_non_empty_account_alarms_and_freezes(tmp_path):
    broker = FakeBroker(accounts=[_account()], rub=Decimal("150000"))
    broker.positions[NG_UID] = 1
    collector, status = _run(tmp_path, broker)
    alarms = [event for event in collector.events if event.get("kind") == "reconcile_alarm"]
    assert len(alarms) == 1 and alarms[0].get("reason") == "Счёт не пуст на первом старте: NG-12.26"
    assert status["frozen"] == ["NG-12.26"]


def test_secrets_not_written(tmp_path):
    broker = FakeBroker(accounts=[_account()], rub=Decimal("150000"))
    _run(tmp_path, broker)
    directory = tmp_path / "contours" / "account"
    for path in directory.iterdir():
        if path.is_file():
            data = path.read_bytes().decode("utf-8", "ignore")
            assert ACCOUNT_ID not in data and TOKEN not in data, path.name


# --- разведка комиссий -------------------------------------------------------------

def _operations(link: bool, *, trades=True):
    executed = s.OperationState.OPERATION_STATE_EXECUTED
    buy = s.OperationItem(id="op-buy-0001-9a8b", type=s.OperationType.OPERATION_TYPE_BUY, state=executed,
                          instrument_uid=NG_UID, commission=money("-26.92"), payment=money("-2691"),
                          trades_info=s.OperationItemTrades(trades=[s.OperationItemTrade(num="tr-1", date=NOW,
                                                                                         quantity=1)] if trades else []),
                          date=NOW)
    fee = s.OperationItem(id="op-fee-0002", type=s.OperationType.OPERATION_TYPE_BROKER_FEE, state=executed,
                          parent_operation_id="op-buy-0001-9a8b" if link else "", instrument_uid=NG_UID,
                          commission=s.MoneyValue(currency="", units=0, nano=0), payment=money("-26.92"), date=NOW)
    return [buy, fee]


def _snap(lots):
    return AccountSnapshot(Decimal(1), None, (("NG-12.26", lots),) if lots else (), (), (), {NG_UID: "NG-12.26"})


@pytest.mark.parametrize("link, trades", [(True, True), (False, False)])
def test_fee_probe_after_foreign_trade(tmp_path, link, trades):
    path = tmp_path / BROKER_DATABASE_FILE
    BrokerStore(path)
    probes = FeeProbes(path)
    broker = FakeBroker(accounts=[_account()])
    broker.operations = _operations(link, trades=trades)
    with _gateway(broker)(TOKEN) as gateway:
        probes.observe(NOW, _snap(0))
        probes.observe(NOW + timedelta(minutes=1), _snap(1))
        probes.run(gateway, ACCOUNT_ID, NOW + timedelta(minutes=5))
        assert read_fee_probes(path) == []
        probes.run(gateway, ACCOUNT_ID, NOW + timedelta(minutes=16))
    [probe] = read_fee_probes(path)
    assert probe["instrument"] == "NG-12.26" and probe["operation"] == "…9a8b"
    assert probe["trades_info"] is trades and probe["broker_fee_linked"] is link
    assert probe["commission"] == "26.92" and (probe["broker_fee"] == "26.92") is link


def test_fee_probe_read_failure_gives_up_after_three(tmp_path):
    path = tmp_path / BROKER_DATABASE_FILE
    BrokerStore(path)
    probes = FeeProbes(path)
    broker = FakeBroker(accounts=[_account()], fail_operations=True)
    with _gateway(broker)(TOKEN) as gateway:
        probes.observe(NOW, _snap(0))
        probes.observe(NOW, _snap(2))
        for minute in (16, 17, 18, 19):
            probes.run(gateway, ACCOUNT_ID, NOW + timedelta(minutes=minute))
    [probe] = read_fee_probes(path)
    assert probe["error"] == "UNAVAILABLE" and probe["operation"] is None


# --- порядок подъёма -------------------------------------------------------------------

def test_broker_contours_recover_in_order_and_failure_does_not_block():
    import run

    order = []

    class Supervisor:
        def __init__(self, kind, fail=False):
            self.kind, self.fail = kind, fail

        def recover(self):
            order.append(self.kind)
            if self.fail:
                raise RuntimeError("нет токена")
            return "autostart"

    run._recover_broker_contours(Supervisor("account", fail=True), Supervisor("sandbox"))
    assert order == ["account", "sandbox"]


# --- карточка отказа -----------------------------------------------------------------

def test_refused_card_is_not_a_reconcile_alarm():
    from src.events.event import Event
    from src.notifier.telegram_templates import render_broker_account

    reason = "Токен видит счёт …7029, а TINKOFF_ACCOUNT_ID задаёт другой"
    text = render_broker_account(Event.broker_account("refused", contour="account", account="…", event_id="e1",
                                                      reason=reason))
    assert text.startswith("<b>СЧЁТ · ТРЕВОГА · Контур не стартует</b>")
    assert reason in text and "Сверка" not in text and "заморозк" not in text and "Счёт …\n" not in text


def test_wrong_account_id_sends_refused_card(tmp_path):
    collector, bus, stop = Collector(), EventBus(), Stop()
    bus.subscribe_all([collector])
    broker = FakeBroker(accounts=[_account("2000555-other-0002")])
    contour = BrokerContour(_settings(tmp_path), session_factory=_sessions(broker, orders=False), bus=bus, stop=stop,
                            pid=4242)
    assert contour.run() == 2
    assert collector.kinds() == ["refused"]
    assert collector.events[0].get("reason") == "Токен видит счёт …0002, а TINKOFF_ACCOUNT_ID задаёт другой"


# --- заявки на счёте (enable-live-trading) ------------------------------------------------

def test_account_with_orders_builds_trading_part(tmp_path):
    from src.assembly.core_state import CoreStateStore, Member
    from src.events.types import EventType
    from tests.unit.broker_contour.test_broker_contour import FakeTradingPart, _run_in_thread, _wait

    broker = FakeBroker(accounts=[_account(level=FULL)], rub=Decimal("150000"))
    store = CoreStateStore(tmp_path / "core.sqlite3")
    store.record_launch("account", "account", [], source="api",
                        members=[Member("future-ng-macd", {"id": "future-ng-macd", "instrument": "NG",
                                                          "strategy": "macd_rsi_stoch", "timeframe": "15m"})])
    collector, stop, bus = Collector(), Stop(), EventBus()
    collector.supported_types = frozenset({EventType.BROKER_ACCOUNT, EventType.BROKER_ORDER})
    bus.subscribe(collector)
    parts = []

    def factory(trading, control, strategies):
        parts.append(FakeTradingPart(trading, control, strategies, stop))
        return parts[-1]

    from dataclasses import replace
    settings = replace(_settings(tmp_path), orders=True)
    contour = BrokerContour(settings, session_factory=_sessions(broker, orders=True), bus=bus, stop=stop,
                            pid=4242, trading_factory=factory)
    thread, result = _run_in_thread(contour)
    status_path = tmp_path / "contours" / "account" / "status.json"
    assert _wait(lambda: status_path.exists() and json.loads(status_path.read_text()).get("stream_link") == "ok")
    status = json.loads(status_path.read_text())
    assert status["orders_enabled"] is True and parts and status["admission"]["access_level"] == "full_access"
    started = next(event for event in collector.events if event.get("kind") == "started")
    assert started.get("orders_enabled") is True and started.get("contour") == "account"
    assert started.get("access_level") == "full_access"
    store.record_stop("account", "no_entries")
    thread.join(timeout=5)
    assert result["code"] == 0
    assert "orders_stream.order_state_stream" in {name for name, _ in broker.calls}
    assert not {name for name, _ in broker.calls if name.startswith("sandbox.")}
