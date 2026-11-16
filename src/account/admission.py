"""Факты допуска контура «Счёт» (add-account-contour, design.md решение 4).

Права токена, допуски брокера и лимиты API. При ``orders = false`` (Р1) нет
допуска к срочным контрактам и сбой ``GetUserTariff`` — предупреждения; при
``orders = true`` (Р2) нужны полный доступ и допуск к срочным контрактам.
"""

from __future__ import annotations

from src.broker_contour.admission import AdmissionError
from src.sandbox.gateway import BrokerCallError

DERIVATIVES = "derivative"       # строка допуска к срочным контрактам; сверить по списку на первом старте
# Лимиты методов, которые вызывает контур: у сервиса операций тяжёлые методы (отчёт брокера) ограничены
# 5 в минуту, и минимум по сервису вводил бы в заблуждение.
LIMIT_METHODS = {"OrdersService/PostOrder": "orders", "StopOrdersService/PostStopOrder": "stop_orders",
                 "OperationsService/GetOperationsByCursor": "operations"}
NO_DERIVATIVES = "нет допуска к срочным контрактам — на Р2 фьючерсы не торгуются"


def _limits(response) -> dict[str, int]:
    """Лимит в минуту методов контура: заявка, стоп-заявка, операции по курсору."""
    found: dict[str, int] = {}
    for limit in response.unary_limits:
        for method in limit.methods:
            service, _, name = method.rpartition("/")
            key = LIMIT_METHODS.get(f"{service.rsplit('.', 1)[-1]}/{name}")
            if key is not None:
                found[key] = min(found.get(key, limit.limit_per_minute), limit.limit_per_minute)
    return dict(sorted(found.items()))


def read_facts(gateway, account, *, orders: bool) -> dict:
    """Права токена, ``GetInfo`` и ``GetUserTariff``; отказ — ``AdmissionError`` с тревогой в канал."""
    tail = account.masked
    level = account.access_level
    if orders and level != "full_access":
        raise AdmissionError("Токен счёта только на чтение, а [contours.account] orders = true: нужен полный доступ",
                             alarm=True, account_tail=tail)
    try:
        info = gateway.call("users.get_info")
    except BrokerCallError as exc:
        raise AdmissionError(f"Допуски брокера не прочитаны (GetInfo: {exc.code})", alarm=True,
                             account_tail=tail) from None
    qualified = sorted(str(item) for item in info.qualified_for_work_with)
    derivatives = DERIVATIVES in qualified
    warnings = []
    if not derivatives:
        if orders:
            raise AdmissionError("Нет допуска к срочным контрактам: заявки по фьючерсам брокер отклонит",
                                 alarm=True, account_tail=tail)
        warnings.append(NO_DERIVATIVES)
    try:
        limits = _limits(gateway.call("users.get_user_tariff"))
    except BrokerCallError as exc:
        limits = None
        warnings.append(f"лимиты API не прочитаны (GetUserTariff: {exc.code})")
    return {"access_level": level, "qualified_for_work_with": qualified, "derivatives": derivatives,
            "tariff": str(info.tariff) or None, "limits": limits, "warnings": warnings}
