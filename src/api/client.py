from t_tech.invest import Client
from src.config import TINKOFF_TOKEN

def get_client():
"""Возвращает клиент Tinkoff Invest API."""
return Client(TINKOFF_TOKEN)