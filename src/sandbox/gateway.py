"""Шлюзы к Т-Инвест переехали в ``src.broker_contour.tinvest.gateway`` (refactor-broker-port).

Реэкспорт для разведки песочницы (``python -m src.sandbox``).
"""

from src.broker_contour.port import BrokerCallError
from src.broker_contour.tinvest.gateway import (ACCOUNT_TARGET, DEADLINE_SECS, RATE_LIMIT_BACKOFF_SECS,
                                                RATE_LIMIT_MAX_WAIT_SECS, RATE_LIMIT_RETRIES, SANDBOX_ONLY,
                                                SANDBOX_TARGET, AccountGateway, BrokerGateway, SandboxGateway,
                                                _account_client, _default_client, _rate_limit_wait, broker_error)

__all__ = ["ACCOUNT_TARGET", "DEADLINE_SECS", "RATE_LIMIT_BACKOFF_SECS", "RATE_LIMIT_MAX_WAIT_SECS",
           "RATE_LIMIT_RETRIES", "SANDBOX_ONLY", "SANDBOX_TARGET", "AccountGateway", "BrokerCallError",
           "BrokerGateway", "SandboxGateway", "_account_client", "_default_client", "_rate_limit_wait",
           "broker_error"]
