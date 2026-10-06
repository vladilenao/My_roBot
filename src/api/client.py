from t_tech.invest import Client

from src.config import TINKOFF_TOKEN
from src.logging_setup import get_logger

log = get_logger(__name__)


class ClientProvider:
    """Порт источника данных: контекстный менеджер клиента выбранного режима.

    Остальной контур (загрузчик свечей, поиск инструмента, выбор инструментов)
    работает с любым провайдером одинаково: меняется только реализация.
    """

    def client_context(self, token=None):
        raise NotImplementedError


class TinkoffClientProvider(ClientProvider):
    """Боевой режим: аутентифицированный клиент T-Банка Invest API."""

    def client_context(self, token=None):
        return Client(token or TINKOFF_TOKEN)


_DEFAULT_PROVIDER = TinkoffClientProvider()


def default_client_provider() -> ClientProvider:
    """Провайдер боевого режима по умолчанию."""
    return _DEFAULT_PROVIDER


def get_client():
    """Возвращает клиента Tinkoff Invest API."""
    return Client(TINKOFF_TOKEN)


def client_context(token=None):
    """Возвращает контекстный менеджер для клиента API."""
    return _DEFAULT_PROVIDER.client_context(token)
