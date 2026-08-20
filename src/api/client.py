from t_tech.invest import Client
from src.config import TINKOFF_TOKEN


def get_client():
    """Возвращает клиент Tinkoff Invest API."""
    return Client(TINKOFF_TOKEN)


def client_context(token=None):
    """Возвращает контекстный менеджер для клиента API."""
    return Client(token or TINKOFF_TOKEN)