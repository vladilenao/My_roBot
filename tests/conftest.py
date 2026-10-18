import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Перезаписать эталонные файлы expected_signals.csv текущим поведением конвейера вместо сверки.",
    )


class _OfflineRedis:
    """Redis недоступен: тесты не ходят в настоящий поток cluster.events."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError("Redis отключён в тестах")

        return fail


@pytest.fixture(autouse=True)
def _offline_cluster_redis(monkeypatch):
    """Кластерный фильтр в тестах прогона не читает локальный Redis разработчика."""
    monkeypatch.setattr("run._cluster_redis", lambda: _OfflineRedis())


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_broker_sdk: тест сам создаёт настоящий Client SDK брокера (без сети и токена); "
        "запрет sentry_sdk.init при этом остаётся",
    )


def _forbidden(what: str):
    def fail(*args, **kwargs):
        pytest.fail(
            f"{what} в тестах запрещён: подменяйте src.api.client.Client "
            "или передавайте свой ClientProvider",
            pytrace=False,
        )

    return fail


@pytest.fixture(autouse=True)
def _no_real_broker_client(request, monkeypatch):
    """Тесты не открывают клиент брокера и не включают его Sentry.

    Настоящий ``Client`` получил бы токен из ``.env`` и канал к боевому адресу,
    а его ``__enter__`` включил бы Sentry T-Bank на весь процесс pytest.
    ``pytest.fail`` — не ``Exception``, его не погасит ``except Exception`` кода.
    """
    import sentry_sdk
    from t_tech.invest import clients

    monkeypatch.setattr(sentry_sdk, "init", _forbidden("sentry_sdk.init (телеметрия SDK брокера)"))
    if request.node.get_closest_marker("real_broker_sdk") is None:
        monkeypatch.setattr(clients.Client, "__init__", _forbidden("Настоящий Client SDK брокера"))
        monkeypatch.setattr(clients.AsyncClient, "__init__", _forbidden("Настоящий AsyncClient SDK брокера"))
