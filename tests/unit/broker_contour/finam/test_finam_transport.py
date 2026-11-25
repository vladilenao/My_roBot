"""Транспорт Финама (add-finam-contour, решения 5–7; tasks 4.1, 4.2): JWT, лимит, окно обслуживания."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import grpc
import pytest

from src.broker_contour.finam import transport as transport_module
from src.broker_contour.finam.transport import clean_details, in_maintenance
from src.broker_contour.port import BrokerCallError, BrokerOutage
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.logging_setup import redact
from tests.unit.broker_contour.finam.fakes import (ACCOUNT_ID, SECRET, WINDOW, FakeFinam, FakeRpcError,
                                                   transport)

REQUEST = accounts_pb2.GetAccountRequest(account_id=ACCOUNT_ID)


def _wait(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("условие не наступило")
        time.sleep(0.005)


def _header(fake, method):
    return [header for name, header in fake.calls if name == method][-1]


def test_auth_service_without_header_and_jwt_on_calls():
    fake = FakeFinam()
    with transport(fake) as link:
        link.call("accounts.GetAccount", REQUEST)
    assert _header(fake, "Auth") is None and _header(fake, "SubscribeJwtRenewal") is None
    assert _header(fake, "GetAccount") in fake.issued


def test_renewal_stream_replaces_jwt_and_makes_it_secret():
    fake = FakeFinam()
    with transport(fake) as link:
        _wait(lambda: link.jwt.renewals == 1)         # первый JWT стрима — сразу после подписки
        fresh = "fake-finam-jwt-renewed-" + "y" * 24
        fake.renewal.put(fresh)
        _wait(lambda: link.jwt.renewals == 2)
        link.call("accounts.GetAccount", REQUEST)
    assert _header(fake, "GetAccount") == fresh
    for token in fake.issued:
        assert token not in redact(f"вызов с {token}")
    assert SECRET not in redact(f"секрет {SECRET}") and ACCOUNT_ID not in redact(f"счёт {ACCOUNT_ID}")


def test_broken_stream_falls_back_to_auth_by_age():
    fake = FakeFinam(renewal_open=False)
    now = [0.0]
    with transport(fake, monotonic=lambda: now[0], backoff=(60,)) as link:
        assert not link.jwt._streaming.is_set()
        now[0] = 700.0
        link.call("accounts.GetAccount", REQUEST)
    assert fake.names().count("Auth") == 2              # вход и замена JWT старше 10 минут
    assert _header(fake, "GetAccount") == fake.issued[-1]


def test_stream_reopens_after_break():
    fake = FakeFinam()
    with transport(fake, backoff=(0.01,)) as link:
        _wait(lambda: link.jwt.renewals == 1)
        fake.renewal.put(FakeRpcError(grpc.StatusCode.UNAVAILABLE, "обрыв"))
        _wait(lambda: fake.renewal_opened == 2)
        _wait(lambda: link.jwt.renewals == 2)


def test_rate_limit_waits_for_quota_reset():
    fake = FakeFinam(reset_in={"GetAccount": 12})
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "Too Many Requests")]
    with transport(fake) as link:
        link.call("accounts.GetAccount", REQUEST)
    assert fake.slept == [13.0]
    assert fake.names().count("GetAccount") == 2 and "GetUsageMetrics" in fake.names()


def test_rate_limit_gives_up_after_two_retries_and_caps_wait():
    fake = FakeFinam(reset_in={"GetAccount": 600})
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "") for _ in range(3)]
    with transport(fake) as link, pytest.raises(BrokerCallError) as exc:
        link.call("accounts.GetAccount", REQUEST)
    assert exc.value.code == "RESOURCE_EXHAUSTED" and fake.slept == [65, 65]


def test_rate_limit_without_quota_waits_a_minute():
    fake = FakeFinam(quotas={})
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "")]
    with transport(fake) as link:
        link.call("accounts.GetAccount", REQUEST)
    assert fake.slept == [60]


def test_unavailable_in_window_is_outage():
    fake = FakeFinam()
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.UNAVAILABLE, "maintenance")]
    with transport(fake, clock=lambda: WINDOW) as link, pytest.raises(BrokerOutage) as exc:
        link.call("accounts.GetAccount", REQUEST)
    assert exc.value.code == "UNAVAILABLE"


def test_unavailable_outside_window_is_plain_error():
    fake = FakeFinam()
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")]
    with transport(fake) as link, pytest.raises(BrokerCallError) as exc:
        link.call("accounts.GetAccount", REQUEST)
    assert not isinstance(exc.value, BrokerOutage)


def test_auth_refused_in_window_is_outage_on_enter():
    fake = FakeFinam()
    fake.failures["Auth"] = [FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, "")]
    with pytest.raises(BrokerOutage):
        with transport(fake, clock=lambda: WINDOW):
            pass


@pytest.mark.parametrize("moment, inside", [
    (datetime(2026, 11, 25, 1, 59, tzinfo=timezone.utc), False),    # 04:59 МСК
    (datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc), True),      # 05:00
    (datetime(2026, 11, 25, 3, 14, tzinfo=timezone.utc), True),     # 06:14
    (datetime(2026, 11, 25, 3, 15, tzinfo=timezone.utc), False),    # 06:15
])
def test_maintenance_window(moment, inside):
    assert in_maintenance(moment) is inside


def test_error_details_one_line_and_masked():
    fake = FakeFinam()
    fake.failures["GetAccount"] = [FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT,
                                                f"INVALID_ARGUMENT: [145]No enough coverage {ACCOUNT_ID}\x0cНедостаток")]
    with transport(fake) as link, pytest.raises(BrokerCallError) as exc:
        link.call("accounts.GetAccount", REQUEST)
    assert exc.value.details == "[145]No enough coverage …3322 / Недостаток"
    assert clean_details("NOT_FOUND: a\x0cб") == "a / б"


def test_close_stops_renewal_thread():
    fake = FakeFinam()
    with transport(fake) as link:
        thread = link.jwt._thread
    assert not thread.is_alive()


def test_no_retry_constants_left_from_probe():
    """Повтор по лимиту — один, в транспорте: никаких пауз 1, 2, 4 с разведки."""
    assert not hasattr(transport_module, "RATE_LIMIT_BACKOFF_SECS")
