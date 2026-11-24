"""Шлюз Финама, JWT и лимиты: заглушки, метаданные, повторы, ``probe-jwt``, ``probe-limits``."""

from __future__ import annotations

import pytest

from src.finam.gateway import FinamGateway
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.finam.records import CallRecorder
from src.sandbox.gateway import BrokerCallError
from tests.unit.finam.fakes import ACCOUNT_ID, SECRET, FakeFinam, gateway_factory, stubs


def test_stubs_import_next_to_tinvest_sdk():
    import t_tech.invest  # noqa: F401

    order = orders_pb2.Order(symbol="SBER@MISX", client_order_id="fp123")
    assert orders_pb2.Order.FromString(order.SerializeToString()).client_order_id == "fp123"


def make(broker: FakeFinam, tmp_path, **kwargs) -> FinamGateway:
    return gateway_factory(broker)(SECRET, account_id=ACCOUNT_ID, recorder=CallRecorder(tmp_path / "r.jsonl"),
                                   **kwargs)


def test_jwt_refreshes_by_age(tmp_path):
    broker = FakeFinam()
    now = [0.0]
    gateway = FinamGateway(SECRET, account_id=ACCOUNT_ID, channel_factory=lambda target: None,
                           stub_factory=stubs(broker), monotonic=lambda: now[0], clock=broker.clock)
    with gateway:
        gateway.call("assets.Clock", assets_pb2.ClockRequest())
        assert broker.auth_calls == 1
        now[0] = 601
        gateway.call("assets.Clock", assets_pb2.ClockRequest())
        assert broker.auth_calls == 2
        assert gateway.jwt_number == 2


def test_jwt_refresh_can_be_disabled(tmp_path):
    broker = FakeFinam()
    now = [0.0]
    gateway = FinamGateway(SECRET, account_id=ACCOUNT_ID, channel_factory=lambda target: None,
                           stub_factory=stubs(broker), monotonic=lambda: now[0], clock=broker.clock,
                           refresh_jwt=False)
    with gateway:
        now[0] = 10_000
        gateway.call("assets.Clock", assets_pb2.ClockRequest())
    assert broker.auth_calls == 1


def test_read_is_retried_on_rate_limit(tmp_path):
    broker = FakeFinam(exhaust_clock_after=0)
    with make(broker, tmp_path) as gateway:
        with pytest.raises(BrokerCallError) as error:
            gateway.call("assets.Clock", assets_pb2.ClockRequest())
    assert error.value.code == "RESOURCE_EXHAUSTED"
    assert broker.clock_calls == 4  # первый вызов и три повтора
    assert error.value.metadata_keys == ["grpc-retry-pushback-ms", "x-ratelimit"]


def test_place_is_not_retried(tmp_path):
    broker = FakeFinam(exhaust_place=True)
    with make(broker, tmp_path) as gateway:
        with pytest.raises(BrokerCallError):
            gateway.call("orders.PlaceOrder", orders_pb2.Order(account_id=ACCOUNT_ID, symbol="SBER@MISX"))
    assert sum(1 for method, _ in broker.calls if method == "orders.PlaceOrder") == 1


def test_error_text_has_code_and_masked_account(tmp_path):
    broker = FakeFinam(repeat_key="reject")
    with make(broker, tmp_path) as gateway:
        order = orders_pb2.Order(account_id=ACCOUNT_ID, symbol="SBER@MISX", type=orders_pb2.ORDER_TYPE_STOP,
                                 client_order_id="fpkey")
        gateway.call("orders.PlaceOrder", order)
        with pytest.raises(BrokerCallError) as error:
            gateway.call("orders.PlaceOrder", order)
    text = str(error.value)
    assert text.startswith("ALREADY_EXISTS")
    assert ACCOUNT_ID not in text and ACCOUNT_ID[-4:] in text
    assert "RAW-RPC-ERROR" not in text


def test_jwt_probe(run, broker):
    result = run("probe-jwt", "--minutes", "20")
    assert result.code == 0, result.report()
    assert "15 мин" in result.row("Ф0.40")[1]
    outcome, detail = result.row("Ф0.41")
    assert outcome == "да" and "jwt#2 на 13.0 мин" in detail
    outcome, detail = result.row("Ф0.42")
    assert outcome == "да" and "на 15-й минуте" in detail and "UNAUTHENTICATED" in detail
    outcome, detail = result.row("Ф0.43")
    assert outcome == "да" and "закрыт" in detail
    assert broker.auth_calls == 1
    text = result.all_files_text()
    assert len(broker.jwts) == 2
    for token in (SECRET, *broker.jwts):
        assert token not in text


def test_limits_probe(run, broker):
    broker.exhaust_clock_after = 200
    result = run("probe-limits")
    assert result.code == 0, result.report()
    outcome, detail = result.row("Ф0.51")
    assert outcome == "да"
    assert "на 201-м вызове Clock" in detail and "grpc-retry-pushback-ms" in detail
    assert broker.clock_calls == 201
    assert result.row("Ф0.50")[0] == "да" and result.row("Ф0.52")[0] == "да"


def test_limits_probe_without_refusal(run, broker):
    result = run("probe-limits", "--calls", "5")
    assert result.row("Ф0.51")[0] == "не проверено"
    assert broker.clock_calls == 5
