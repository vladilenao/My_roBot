"""Общее подключение к брокеру для unary-вызовов (fix-broker-channel-reuse).

20.11.2026: новое gRPC-подключение к T-Invest примерно через раз
устанавливается ~2 с, открытое отвечает за десятки миллисекунд. Ядро открывало
новое на каждый запрос свечей — эмуляция ждала прогрева ~4 минуты.
"""

import threading
from types import SimpleNamespace

import grpc
import pytest
from t_tech.invest.exceptions import RequestError

from src.api.client import TinkoffClientProvider


class _FakeClient:
    def __init__(self, token):
        self.token = token
        self.services = SimpleNamespace(token=token)
        self.closed = False

    def __enter__(self):
        return self.services

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture
def opened():
    return []


@pytest.fixture
def provider(opened):
    def factory(token):
        client = _FakeClient(token)
        opened.append(client)
        return client

    return TinkoffClientProvider(client_factory=factory)


def test_calls_share_one_connection_and_exit_keeps_it_open(provider, opened):
    with provider.client_context("token-a") as first:
        pass
    with provider.client_context("token-a") as second:
        pass

    assert first is second
    assert len(opened) == 1
    assert not opened[0].closed


def test_tokens_do_not_share_connection(provider, opened):
    with provider.client_context("token-a") as first, provider.client_context("token-b") as second:
        assert (first.token, second.token) == ("token-a", "token-b")
    assert len(opened) == 2


def test_parallel_calls_open_one_connection(provider, opened):
    barrier = threading.Barrier(8)

    def call():
        barrier.wait()
        with provider.client_context("token-a"):
            pass

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(opened) == 1


@pytest.mark.parametrize("error", [
    RequestError(grpc.StatusCode.UNAVAILABLE, "connection reset", None),
    type("RawRpcError", (grpc.RpcError,), {"code": lambda self: grpc.StatusCode.UNAVAILABLE})(),
])
def test_unavailable_drops_connection(provider, opened, error):
    with pytest.raises(type(error)):
        with provider.client_context("token-a"):
            raise error
    with provider.client_context("token-a"):
        pass

    assert len(opened) == 2


def test_other_errors_keep_connection(provider, opened):
    with pytest.raises(RequestError):
        with provider.client_context("token-a"):
            raise RequestError(grpc.StatusCode.RESOURCE_EXHAUSTED, "limit", None)
    with pytest.raises(ValueError):
        with provider.client_context("token-a"):
            raise ValueError("bad frame")
    with provider.client_context("token-a"):
        pass

    assert len(opened) == 1


def test_close_closes_and_next_call_reopens(provider, opened):
    with provider.client_context("token-a"):
        pass

    provider.close()

    assert opened[0].closed
    with provider.client_context("token-a"):
        pass
    assert len(opened) == 2 and not opened[1].closed


def test_stream_session_gets_own_connection(provider, opened):
    with provider.client_context("token-a"):
        pass

    session = provider.stream_session("token-a")

    assert len(opened) == 2
    assert session._context is opened[1]
