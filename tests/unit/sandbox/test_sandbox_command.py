"""Команда песочницы: запуск, счёт робота, ручные действия, шлюз (add-sandbox-probe)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from t_tech.invest import schemas as s

from src.sandbox import __main__ as command
from src.sandbox.gateway import SANDBOX_TARGET, SandboxGateway, _default_client
from src.sandbox.records import CallRecorder
from tests.unit.sandbox.fakes import ACCOUNT_ID, NG_UID, TOKEN, FakeBroker, FakeClient, robot_account


def _factory(broker: FakeBroker):
    def make(token, recorder=None):
        return SandboxGateway(token, client_factory=lambda value: FakeClient(broker, value), recorder=recorder,
                              sleep=lambda seconds: None)
    return make


def _run(broker, argv, tmp_path, token=TOKEN):
    lines: list[str] = []
    code = command.main(argv, token=token, gateway_factory=_factory(broker), out=lines.append, base_dir=tmp_path)
    return code, lines


def _methods(broker) -> list[str]:
    return [name for name, _ in broker.calls]


def test_without_token_no_network(tmp_path):
    broker = FakeBroker()
    code, lines = _run(broker, ["account"], tmp_path, token=None)
    assert code == 2
    assert lines == ["Не задан TINKOFF_SANDBOX_TOKEN"]
    assert broker.calls == []


def test_without_action_prints_actions(tmp_path):
    code, lines = _run(FakeBroker(), [], tmp_path)
    assert code == 2
    assert "pay-in <рубли>" in lines[0] and "probe" in lines[0]


def test_bad_amount_and_lots_no_network(tmp_path):
    broker = FakeBroker()
    assert _run(broker, ["pay-in", "-5"], tmp_path)[0] == 2
    assert _run(broker, ["pay-in", "abc"], tmp_path)[0] == 2
    assert _run(broker, ["buy", "NG", "0"], tmp_path)[0] == 2
    assert broker.calls == []


def test_first_run_opens_robot_account(tmp_path):
    broker = FakeBroker()
    code, lines = _run(broker, ["account"], tmp_path)
    assert code == 0
    assert lines[0] == "Открыт счёт песочницы …a1b2"
    assert "Рубли: 0 ₽" in lines
    assert {"Позиции: нет", "Заявки: нет", "Стоп-заявки: нет"} <= set(lines)
    assert (tmp_path / "sandbox" / "sandbox.log").exists()
    assert not any(ACCOUNT_ID in line for line in lines)


def test_two_robot_accounts_refused(tmp_path):
    broker = FakeBroker(accounts=[
        s.Account(id="sbx-first-0001", name="My_roBot", status=s.AccountStatus.ACCOUNT_STATUS_OPEN),
        s.Account(id="sbx-second-0002", name="My_roBot", status=s.AccountStatus.ACCOUNT_STATUS_OPEN),
    ])
    code, lines = _run(broker, ["buy", "NG", "1"], tmp_path)
    assert code == 1
    assert "…0001" in lines[-1] and "…0002" in lines[-1]
    assert "orders.post_order" not in _methods(broker)


def test_other_accounts_untouched(tmp_path):
    broker = FakeBroker(accounts=[
        s.Account(id="sbx-other-9999", name="Чужой", status=s.AccountStatus.ACCOUNT_STATUS_OPEN)])
    code, _ = _run(broker, ["account"], tmp_path)
    assert code == 0
    assert "sandbox.open_sandbox_account" in _methods(broker)
    assert all(kwargs.get("account_id") != "sbx-other-9999" for _, kwargs in broker.calls)


def test_pay_in(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    code, lines = _run(broker, ["pay-in", "1000000"], tmp_path)
    assert code == 0
    assert lines[-1].endswith("Баланс: 1 000 000 ₽")


def test_manual_buy_by_family_code(tmp_path):
    broker = FakeBroker(rub=Decimal(1_000_000))
    robot_account(broker)
    code, lines = _run(broker, ["buy", "ng", "1"], tmp_path)
    assert code == 0
    assert lines[0].startswith("NG → NG-12.26, до экспирации")
    assert lines[-1] == "Заявка исполнена: исполнено 1 из 1, цена 3 ₽, комиссия 1.50 ₽"
    assert broker.positions == {NG_UID: 1}
    assert not any("NGZ6" in line for line in lines)
    code, lines = _run(broker, ["account"], tmp_path)
    assert "  NG-12.26 1 лот, средняя 3 ₽" in lines


def test_exchange_ticker_refused(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    code, lines = _run(broker, ["buy", "NGZ6", "1"], tmp_path)
    assert code == 2
    assert lines[-1] == "Биржевой тикер не принимается — используйте NG-12.26."
    assert "orders.post_order" not in _methods(broker)


def test_broker_reject_printed_by_code(tmp_path):
    broker = FakeBroker(reject_market=True)
    robot_account(broker)
    code, lines = _run(broker, ["sell", "SBER", "1"], tmp_path)
    assert code == 1
    assert lines[-1] == "Не выполнено: INVALID_ARGUMENT 30079: Инструмент недоступен для торгов"


def test_reset_cleans_everything(tmp_path):
    broker = FakeBroker(rub=Decimal(1_000_000))
    robot_account(broker)
    _run(broker, ["buy", "NG", "2"], tmp_path)
    api = FakeClient(broker, TOKEN).__enter__()
    api.orders.post_order(instrument_id=NG_UID, quantity=1, price=s.Quotation(units=2, nano=0),
                          direction=s.OrderDirection.ORDER_DIRECTION_BUY, account_id=ACCOUNT_ID,
                          order_type=s.OrderType.ORDER_TYPE_LIMIT, order_id="manual-key")
    api.stop_orders.post_stop_order(instrument_id=NG_UID, quantity=2, stop_price=s.Quotation(units=2, nano=0),
                                    direction=s.StopOrderDirection.STOP_ORDER_DIRECTION_SELL, account_id=ACCOUNT_ID,
                                    stop_order_type=s.StopOrderType.STOP_ORDER_TYPE_STOP_LOSS)
    broker.closed = False
    code, lines = _run(broker, ["reset"], tmp_path)
    assert code == 0
    assert lines[0].startswith("Внимание: работающий контур «Песочница»")
    assert {"Позиции: нет", "Заявки: нет", "Стоп-заявки: нет"} <= set(lines)
    assert broker.positions == {}


@pytest.mark.real_broker_sdk
def test_default_client_is_sandbox_only():
    client = _default_client(TOKEN)
    assert client._target == SANDBOX_TARGET == "sandbox-invest-public-api.tbank.ru"


def test_recorder_hides_account_and_metadata(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    recorder = CallRecorder(tmp_path / "responses.jsonl")
    with _factory(broker)(TOKEN, recorder=recorder) as gateway:
        gateway.set_account(ACCOUNT_ID)
        gateway.call("operations.get_positions", account_id=ACCOUNT_ID)
        try:
            gateway.call("instruments.get_instrument_by", id="NOPE")
        except Exception as exc:  # noqa: BLE001
            assert str(exc) == "NOT_FOUND 50002: Инструмент не найден"
    text = recorder.path.read_text(encoding="utf-8")
    assert ACCOUNT_ID not in text and TOKEN not in text
    entries = [json.loads(line) for line in text.splitlines()]
    assert entries[0]["request"] == {"account_id": "sandbox-account"}
    assert entries[1]["error"]["code"] == "NOT_FOUND"
    assert all("metadata" not in entry for entry in entries)
