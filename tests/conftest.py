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
