"""Unit-тесты транспорта Bot API: retryable только при транспортной ошибке."""

import pytest
import requests

from src.notifier.telegram_transport import TelegramTransport


class Server:
    def __init__(self, body=None, failure=None, status=200):
        self.body = body
        self.failure = failure
        self.status = status

    def post(self, url, data, timeout, files=None):
        if self.failure is not None:
            raise self.failure

        body = self.body

        class Response:
            status_code = self.status

            def json(self):
                return body

        return Response()


@pytest.fixture
def transport(monkeypatch):
    instance = TelegramTransport("https://proxy.test", "123:secret", "-10012345", 10)
    return instance, monkeypatch


def test_transport_exception_is_uncertain_and_retryable(transport):
    instance, monkeypatch = transport
    server = Server(failure=requests.Timeout("https://proxy.test/bot123:secret/sendPhoto"))
    monkeypatch.setattr(requests, "post", server.post)

    result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.ok, result.uncertain, result.retryable) == (False, True, True)


def test_failed_body_is_not_retryable(transport):
    instance, monkeypatch = transport
    server = Server(body={"ok": False, "description": "bad photo"})
    monkeypatch.setattr(requests, "post", server.post)

    result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.ok, result.uncertain, result.retryable) == (False, False, False)


def test_rate_limited_body_is_not_retryable(transport):
    instance, monkeypatch = transport
    server = Server(body={"ok": False, "parameters": {"retry_after": 12}}, status=429)
    monkeypatch.setattr(requests, "post", server.post)

    result = instance.request("sendMessage", {"text": "x"})

    assert (result.ok, result.retryable) == (False, False)


def test_confirmed_success_is_not_retryable(transport):
    instance, monkeypatch = transport
    server = Server(body={"ok": True, "result": {"message_id": 7}})
    monkeypatch.setattr(requests, "post", server.post)

    result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.ok, result.uncertain, result.retryable) == (True, False, False)
    assert result.result["message_id"] == 7


def test_unparsable_success_without_message_id_is_uncertain_but_not_retryable(transport):
    instance, monkeypatch = transport
    server = Server(body={"ok": True, "result": {}})
    monkeypatch.setattr(requests, "post", server.post)

    result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.ok, result.uncertain, result.retryable) == (False, True, False)
