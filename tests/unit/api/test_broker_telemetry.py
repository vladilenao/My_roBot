"""Телеметрия SDK брокера (Sentry T-Bank) отключена, тесты не открывают настоящий клиент."""
import asyncio
import logging

import pytest
import sentry_sdk
from t_tech.invest import clients

import src.api.client as api_client


def test_import_replaces_sdk_telemetry_hooks():
    assert clients.init_error_hub is api_client._broker_telemetry_off
    assert clients.async_init_error_hub is api_client._async_broker_telemetry_off


def test_replaced_hooks_do_not_start_sentry():
    # sentry_sdk.init в тестах запрещён conftest: вызов провалил бы тест.
    assert clients.init_error_hub(object()) is None
    assert asyncio.run(clients.async_init_error_hub(object())) is None


@pytest.mark.real_broker_sdk
def test_entering_real_sdk_client_keeps_sentry_off():
    # Канал gRPC ленивый: без RPC соединения нет, токен поддельный.
    with clients.Client("fake-token-for-tests", target="127.0.0.1:9"):
        assert not sentry_sdk.get_client().is_active()


@pytest.mark.real_broker_sdk
def test_entering_real_async_sdk_client_keeps_sentry_off():
    async def enter():
        async with clients.AsyncClient("fake-token-for-tests", target="127.0.0.1:9"):
            return sentry_sdk.get_client().is_active()

    assert asyncio.run(enter()) is False


def test_missing_hooks_are_reported(monkeypatch, caplog):
    monkeypatch.delattr(clients, "init_error_hub")

    with caplog.at_level(logging.WARNING, logger="src.api.client"):
        assert api_client.disable_broker_telemetry() is False

    assert "точка включения телеметрии не найдена" in caplog.text


def test_disable_is_idempotent():
    assert api_client.disable_broker_telemetry() is True
    assert api_client.disable_broker_telemetry() is True
    assert clients.init_error_hub is api_client._broker_telemetry_off


class TestConftestGuard:
    def test_real_client_is_forbidden(self):
        with pytest.raises(pytest.fail.Exception, match="Настоящий Client SDK брокера"):
            clients.Client("fake-token-for-tests")

    def test_real_async_client_is_forbidden(self):
        with pytest.raises(pytest.fail.Exception, match="Настоящий AsyncClient SDK брокера"):
            clients.AsyncClient("fake-token-for-tests")

    def test_sentry_init_is_forbidden(self):
        with pytest.raises(pytest.fail.Exception, match="sentry_sdk.init"):
            sentry_sdk.init(dsn=None)
