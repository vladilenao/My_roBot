"""Команда ``python -m src.finam``: запуск, выбор счёта, ``account``, ``cleanup``, секреты (add-finam-probe)."""

from __future__ import annotations

from decimal import Decimal

from src.finam.proto.grpc.tradeapi.v1 import side_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from tests.unit.finam.conftest import default_env
from tests.unit.finam.fakes import ACCOUNT_ID, DEMO_ACCOUNT_ID, NG, SECRET, d, stamp


def network_calls(broker) -> list[str]:
    return [method for method, _ in broker.calls]


def test_without_action_prints_usage(run, broker):
    result = run()
    assert result.code == 2
    assert "account" in result.text and "probe-trigger" in result.text
    assert broker.calls == []


def test_unknown_action_and_bad_params(run, broker):
    assert run("buy").code == 2
    result = run("probe", "--offset-pct", "0")
    assert result.code == 2 and "Ошибка параметров" in result.text
    assert broker.calls == []


def test_no_secret_no_network(run, broker):
    result = run("account", env=default_env(secret=None))
    assert result.code == 2
    assert "Не задан FINAM_SECRET_TOKEN" in result.text
    assert broker.calls == []


def test_no_account_no_network(run, broker):
    result = run("account", env=default_env(account_id=None))
    assert result.code == 2
    assert "Не задан FINAM_ACCOUNT_ID" in result.text
    assert broker.calls == []


def test_demo_uses_main_secret_when_demo_secret_missing(run, broker):
    result = run("account", "SBER", "--demo")
    assert result.code == 0
    assert DEMO_ACCOUNT_ID[-4:] in result.text
    assert '"account_id": "finam-demo-account"' in result.records()


def test_account_not_in_token(run, broker):
    broker.account_ids = ["OTHER-ACCOUNT-0001"]
    result = run("account")
    assert result.code == 1
    outcome, detail = result.row("Ф0.1")
    assert outcome == "нет" and "…0001" in detail
    assert network_calls(broker) == ["auth.Auth", "auth.TokenDetails"]


def test_auth_failure_writes_report(run, broker):
    result = run("account", env=default_env(secret="wrong-secret-value-123456"))
    assert result.code == 1
    outcome, detail = result.row("Ф0.1")
    assert outcome == "ошибка" and "UNAUTHENTICATED" in detail
    assert "RAW-RPC-ERROR" not in result.all_files_text()


def test_readonly_token_forbids_orders(run, broker):
    broker.readonly = True
    result = run("probe")
    assert result.code == 2
    assert "orders.PlaceOrder" not in network_calls(broker)
    assert "только на чтение" in result.report()


def test_trigger_requires_real_flag_before_network(run, broker):
    result = run("probe-trigger", "NG")
    assert result.code == 2
    assert "--real" in result.text
    assert broker.calls == []


def test_trigger_needs_confirmation(run, broker):
    result = run("probe-trigger", "NG", "--demo", answer="да")
    assert result.code == 2
    assert "Отменено" in result.text
    assert broker.calls == []


def test_account_reads_everything(run, broker):
    broker.positions[NG] = Decimal(1)
    result = run("account", "NG", "SBER")
    assert result.code == 0, result.report()
    for code in ("Ф0.1", "Ф0.2", "Ф0.3", "Ф0.4", "Ф0.5", "Ф0.6", "Ф0.7", "Ф0.8", "Ф0.9"):
        assert result.row(code)[0] == "да", (code, result.row(code))
    instruments = result.row("Ф0.6")[1]
    assert "NG-12.26: символ найден, площадка RTSX" in instruments
    assert "лот совпал, шаг совпал, стоимость шага — нет в API Финама" in instruments
    assert "одна шкала" in result.row("Ф0.7")[1]
    assert "NG-12.26 quantity 1" in result.row("Ф0.2")[1]
    assert "NG-12.26: UTC CORE_TRADING 09:00–12:00; дни в ответе: Tue 24.11" in result.row("Ф0.8")[1]
    assert "SBER: как у NG-12.26" in result.row("Ф0.8")[1]
    assert "квот 1, лимит 200 у 1; израсходовано: assets.Clock 199 из 200" in result.row("Ф0.9")[1]
    # Только чтение: ни одной заявки.
    assert not {"orders.PlaceOrder", "orders.CancelOrder"} & set(network_calls(broker))
    # Биржевой тикер фьючерса и символ Финама — только в записях вызовов.
    assert "NGZ6" not in result.text and "NGZ6" not in result.report()
    assert NG in result.records()
    assert '"assets_count": 3' in result.records()


def test_futures_without_tinkoff_token(run, broker):
    result = run("account", "NG", "SBER", env=default_env(tinkoff_token=None))
    detail = result.row("Ф0.6")[1]
    assert "для фьючерса нужен TINKOFF_TOKEN" in detail
    assert "SBER: символ найден, площадка MISX" in detail
    assert "карточка Т-Инвест: не проверено" in detail


def test_jwt_is_sent_in_metadata(run, broker):
    run("account", "SBER")
    jwts = [meta.get("authorization") for method, meta in broker.calls if not method.startswith("auth.")]
    assert jwts and all(token in broker.jwts for token in jwts)
    # AuthService получает секрет и JWT только в теле запроса.
    assert all(not meta for method, meta in broker.calls if method.startswith("auth."))
    assert broker.auth_calls == 1


def test_cleanup_cancels_only_probe_orders(run, broker):
    order = orders_pb2.Order(account_id=ACCOUNT_ID, symbol=NG, quantity=d(1), side=side_pb2.SIDE_BUY,
                             type=orders_pb2.ORDER_TYPE_STOP)
    for order_id, key in (("o1", "fpabc123"), ("o2", "manual-777")):
        state = orders_pb2.OrderState(order_id=order_id, status=orders_pb2.ORDER_STATUS_WATCHING,
                                      transact_at=stamp(broker.now))
        state.order.CopyFrom(order)
        state.order.client_order_id = key
        broker.orders[order_id] = state
    result = run("cleanup")
    assert result.code == 0
    assert broker.orders["o1"].status == orders_pb2.ORDER_STATUS_CANCELED
    assert broker.orders["o2"].status == orders_pb2.ORDER_STATUS_WATCHING
    assert "Чужих активных заявок: 1" in result.text


def test_secrets_never_leak(run, broker):
    result = run("probe", "NG")
    assert result.code == 0, result.report()
    text = result.all_files_text()
    assert broker.jwts, "JWT выдавались"
    for value in (SECRET, ACCOUNT_ID, DEMO_ACCOUNT_ID, *broker.jwts):
        assert value not in text
    assert "jwt#1" in result.records()
    assert "[secret]" in result.records()
    assert "finam-account" in result.records()
    assert f"…{ACCOUNT_ID[-4:]}" in result.text


def test_finam_secrets_masked_by_robot_logging():
    import ast
    import inspect

    import run as robot

    source = inspect.getsource(robot)
    secrets = next(node.value for node in ast.parse(source).body
                   if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_SECRETS")
    names = {item.func.id if isinstance(item, ast.Call) else item.id for item in secrets.elts}
    assert {"FINAM_SECRET_TOKEN", "FINAM_ACCOUNT_ID", "FINAM_DEMO_SECRET_TOKEN", "FINAM_DEMO_ACCOUNT_ID"} <= names


def test_cleanup_by_order_number(run, broker):
    """24.11: на другой день стоп разведки пришёл без client_order_id — снять его можно только по номеру."""
    broker.trigger_after_polls = None
    for order_id, key in (("o1", ""), ("o2", "manual-777")):
        state = orders_pb2.OrderState(order_id=order_id, status=orders_pb2.ORDER_STATUS_WATCHING)
        state.order.CopyFrom(orders_pb2.Order(account_id=ACCOUNT_ID, symbol=NG, quantity=d(1),
                                              side=side_pb2.SIDE_BUY, type=orders_pb2.ORDER_TYPE_STOP,
                                              client_order_id=key))
        broker.orders[order_id] = state
    assert run("cleanup").code == 0
    assert broker.orders["o1"].status == orders_pb2.ORDER_STATUS_WATCHING  # без ключа fp… не тронута
    result = run("cleanup", "--order", "o1")
    assert result.code == 0
    assert "Заявка: NG-12.26 STOP покупка 1" in result.text and "Снята: статус CANCELED" in result.text
    assert broker.orders["o1"].status == orders_pb2.ORDER_STATUS_CANCELED
    assert broker.orders["o2"].status == orders_pb2.ORDER_STATUS_WATCHING
    assert run("cleanup", "--order", "nope").code == 1
