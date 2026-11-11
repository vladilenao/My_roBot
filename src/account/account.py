"""Счёт робота на брокере (add-account-contour, design.md решение 3).

Контур «Счёт» не ищет и не открывает счёт: токен видит ровно один открытый
счёт, и это ``TINKOFF_ACCOUNT_ID``. Полные номера наружу не выходят — только
последние четыре символа.
"""

from __future__ import annotations

from t_tech.invest.schemas import AccessLevel, AccountStatus

from src.broker_contour.admission import AccountRuleError
from src.logging_setup import register_secrets
from src.sandbox.account import RobotAccount
from src.sandbox.records import mask_account

ACCESS_LEVELS = {
    AccessLevel.ACCOUNT_ACCESS_LEVEL_FULL_ACCESS: "full_access",
    AccessLevel.ACCOUNT_ACCESS_LEVEL_READ_ONLY: "read_only",
}




def account_by_id(gateway, account_id: str) -> RobotAccount:
    """``GetAccounts``: открытый счёт ровно один, его номер — ``account_id``, права — полный доступ или чтение."""
    register_secrets([account_id])
    response = gateway.call("users.get_accounts")
    opened = [item for item in response.accounts if item.status == AccountStatus.ACCOUNT_STATUS_OPEN]
    register_secrets([item.id for item in response.accounts])
    if len(opened) != 1:
        tails = ", ".join(mask_account(item.id) for item in opened) or "—"
        raise AccountRuleError(f"Токен видит открытых счетов: {len(opened)} ({tails}), а нужен ровно один — "
                               "счёт робота. Выпустите токен только на него.")
    found = opened[0]
    if found.id != account_id:
        raise AccountRuleError(f"Токен видит счёт {mask_account(found.id)}, а TINKOFF_ACCOUNT_ID задаёт другой")
    level = ACCESS_LEVELS.get(found.access_level)
    if level is None:
        raise AccountRuleError(f"У токена нет доступа к счёту {mask_account(found.id)}")
    gateway.set_account(found.id)
    return RobotAccount(found.id, opened=False, access_level=level)
