"""Факты допуска контура «Счёт Финам» (add-finam-contour, решение 9).

Общая часть фактов контура вида «счёт» — права токена, лимиты, предупреждения. Полей Т-Инвест
(допуски, срочный рынок, тариф) у Финама нет и выдуманными значениями они не заполняются.
"""

from __future__ import annotations

from src.broker_contour.admission import AdmissionError
from src.broker_contour.finam.account import FULL_ACCESS
from src.broker_contour.port import BrokerCallError, BrokerOutage

# Методы, которые вызывает контур: квота каждого в минуту (у Финама 200 на метод, факт Ф0 з).
LIMIT_METHODS = ("GetAccount", "GetOrders", "PlaceOrder", "CancelOrder")


def read_facts(transport, account, *, orders: bool) -> dict:
    """Права по ``TokenDetails.readonly`` и квоты ``GetUsageMetrics``; отказ — ``AdmissionError`` с тревогой."""
    if orders and account.access_level != FULL_ACCESS:
        raise AdmissionError("Токен Финама только на чтение, а [contours.finam] orders = true: нужен токен с заявками",
                             alarm=True, account_tail=account.masked)
    warnings = []
    try:
        quotas = transport.quotas()
        limits = {method: quotas[method.lower()] for method in LIMIT_METHODS if method.lower() in quotas}
    except BrokerOutage:
        raise
    except BrokerCallError as exc:
        limits = None
        warnings.append(f"квоты API не прочитаны (GetUsageMetrics: {exc.code})")
    return {"access_level": account.access_level, "limits": limits, "warnings": warnings}
