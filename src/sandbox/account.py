"""Счёт робота в песочнице: открытый счёт с именем ``My_roBot``."""

from __future__ import annotations

from dataclasses import dataclass

from t_tech.invest.schemas import AccountStatus

from src.logging_setup import register_secrets
from src.sandbox.gateway import SandboxGateway
from src.sandbox.records import mask_account

ROBOT_ACCOUNT_NAME = "My_roBot"


class AccountConflict(Exception):
    """Счетов песочницы с именем робота больше одного: какой закрыть, решает человек."""

    def __init__(self, masked: list[str]):
        super().__init__(masked)
        self.masked = masked

    def __str__(self) -> str:
        return (f"Счетов песочницы {ROBOT_ACCOUNT_NAME} несколько: {', '.join(self.masked)}. "
                "Оставьте один, остальные закройте.")


@dataclass(frozen=True)
class RobotAccount:
    account_id: str
    opened: bool

    @property
    def masked(self) -> str:
        return mask_account(self.account_id)


def robot_account(gateway: SandboxGateway) -> RobotAccount:
    """Найти счёт робота; нет — открыть; несколько — ``AccountConflict``.

    Полный номер регистрируется секретом лога и заменой в записях разведки.
    """
    response = gateway.call("users.get_accounts")
    ours = [account for account in response.accounts
            if account.name == ROBOT_ACCOUNT_NAME and account.status == AccountStatus.ACCOUNT_STATUS_OPEN]
    if len(ours) > 1:
        register_secrets([account.id for account in ours])
        raise AccountConflict([mask_account(account.id) for account in ours])
    if ours:
        result = RobotAccount(ours[0].id, opened=False)
    else:
        opened = gateway.call("sandbox.open_sandbox_account", name=ROBOT_ACCOUNT_NAME)
        result = RobotAccount(opened.account_id, opened=True)
    register_secrets([result.account_id])
    gateway.set_account(result.account_id)
    return result
