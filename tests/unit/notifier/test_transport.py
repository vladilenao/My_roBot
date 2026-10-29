"""Unit-тесты транспорта Bot API: retryable только при транспортной ошибке."""

import logging
import socket
import threading
import time

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError

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


# --- Отказ до отправки (unsent) против неопределённого исхода -----------------
# Реальные исключения requests/urllib3 на локальных сокетах: форма исключения —
# единственный признак того, ушёл ли запрос.


def _closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _server(handler):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve():
        connection, _ = listener.accept()
        connection.recv(65536)
        handler(connection)
        connection.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


def test_refused_connection_is_unsent_and_log_has_no_secrets(caplog):
    port = _closed_port()
    instance = TelegramTransport(f"http://127.0.0.1:{port}", "123:secret", "-10012345", 2)

    with caplog.at_level(logging.WARNING):
        result = instance.request("sendMessage", {"text": "x"})

    assert (result.ok, result.unsent, result.uncertain, result.retryable) == (False, True, False, True)
    # Windows не отказывает в подключении сразу, а повторяет SYN до тайм-аута.
    assert result.detail in {"ConnectionError/NewConnectionError", "ConnectTimeout/ConnectTimeoutError"}
    assert "secret" not in caplog.text and str(port) not in caplog.text
    assert result.detail in caplog.text


def test_connection_closed_after_request_is_uncertain(caplog):
    port = _server(lambda connection: None)
    instance = TelegramTransport(f"http://127.0.0.1:{port}", "123:secret", "-10012345", 2)

    with caplog.at_level(logging.WARNING):
        result = instance.request("sendMessage", {"text": "x"})

    assert (result.ok, result.unsent, result.uncertain, result.retryable) == (False, False, True, True)
    assert result.detail == "ConnectionError/ProtocolError"
    assert "secret" not in caplog.text and str(port) not in caplog.text


def test_silent_server_read_timeout_is_uncertain():
    port = _server(lambda connection: time.sleep(1))
    instance = TelegramTransport(f"http://127.0.0.1:{port}", "123:secret", "-10012345", 0.3)

    result = instance.request("sendMessage", {"text": "x"})

    assert (result.unsent, result.uncertain) == (False, True)
    assert result.detail.startswith("ReadTimeout")


@pytest.mark.parametrize("failure, detail", [
    (requests.ConnectTimeout(MaxRetryError(None, "/bot123:secret/sendPhoto",
                                           NewConnectionError(None, "https://proxy.test timed out"))),
     "ConnectTimeout/NewConnectionError"),
    (requests.ConnectionError(MaxRetryError(None, "/bot123:secret/sendPhoto",
                                            NameResolutionError("proxy.test", None, socket.gaierror()))),
     "ConnectionError/NameResolutionError"),
])
def test_connect_timeout_and_dns_failure_are_unsent(transport, failure, detail, caplog):
    instance, monkeypatch = transport
    monkeypatch.setattr(requests, "post", Server(failure=failure).post)

    with caplog.at_level(logging.WARNING):
        result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.unsent, result.uncertain, result.detail) == (True, False, detail)
    assert "secret" not in caplog.text and "proxy.test" not in caplog.text


def test_tls_failure_inside_retry_wrapper_stays_uncertain(transport):
    instance, monkeypatch = transport
    failure = requests.exceptions.SSLError(MaxRetryError(None, "/bot123:secret/sendPhoto", OSError("tls")))
    monkeypatch.setattr(requests, "post", Server(failure=failure).post)

    result = instance.request("sendPhoto", {"caption": "x"}, b"\x89PNG")

    assert (result.unsent, result.uncertain) == (False, True)
