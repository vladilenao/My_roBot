from unittest.mock import patch, ANY
from types import SimpleNamespace

import pytest

from src.api.client import client_context, get_client
from src.api.deadline import RpcDeadline


@pytest.fixture
def mock_client_cls():
    with patch("src.api.client.Client") as client_cls:
        yield client_cls


@pytest.fixture
def configured_token():
    with patch("src.api.client.TINKOFF_TOKEN", "env-token"):
        yield "env-token"


class TestGetClient:
    def test_uses_config_token(self, mock_client_cls, configured_token):
        client = get_client()

        mock_client_cls.assert_called_once_with("env-token")
        assert client is mock_client_cls.return_value


class TestClientContext:
    def test_default_falls_back_to_config_token(self, mock_client_cls, configured_token):
        ctx = client_context()

        mock_client_cls.assert_called_once_with("env-token", interceptors=[ANY])
        assert ctx is mock_client_cls.return_value

    def test_explicit_token_wins_over_config(self, mock_client_cls, configured_token):
        client_context(token="custom-token")

        mock_client_cls.assert_called_once_with("custom-token", interceptors=[ANY])

    def test_empty_string_token_falls_back_to_config(self, mock_client_cls, configured_token):
        client_context(token="")

        mock_client_cls.assert_called_once_with("env-token", interceptors=[ANY])


@pytest.mark.parametrize("existing,expected", [(None, 30), (60, 30), (5, 5)])
def test_rpc_deadline_is_finite_and_preserves_shorter_timeout(existing, expected):
    details = SimpleNamespace(method="/GetCandles", timeout=existing, metadata=(), credentials=None)
    captured = RpcDeadline().intercept_unary_unary(lambda details, _: details, details, object())
    assert captured.timeout == expected
    assert captured.method == "/GetCandles"
