"""Счёт робота у Т-Инвест (refactor-broker-port): правила счёта песочницы и «Счёта», снимок счёта.

Снимок — деньги, позиции, активные заявки и стоп-заявки коротким именем. Правило песочницы —
открытый счёт с именем ``My_roBot`` (нет — открыть); правило «Счёта» (add-account-contour, design.md
решение 3) — токен видит ровно один открытый счёт, и это ``TINKOFF_ACCOUNT_ID``. Полные номера наружу
не выходят — только последние четыре символа.
"""

from __future__ import annotations

from t_tech.invest.schemas import AccessLevel, AccountStatus
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal

from src.broker_contour.port import (AccountRuleError, AccountSnapshot, BrokerCallError,  # noqa: F401
                                     RobotAccount, mask_account)
from src.logging_setup import get_logger, register_secrets
from src.sandbox.actions import active_orders, active_stop_orders, portfolio_positions, rub_of
from src.sandbox.instruments import short_name_by_uid

log = get_logger(__name__)

# Код «снимок противоречив» (fix-account-snapshot-cross-check): портфель и позиции брокера расходятся.
INCONSISTENT = "INCONSISTENT"


def read_account(gateway, account: RobotAccount, names: dict | None = None) -> AccountSnapshot:
    """Прочитать счёт целиком; сбой брокера — ``BrokerCallError`` наружу (сверка пропускается).

    Позиции ``GetPortfolio`` перепроверяются по ``GetPositions`` (fix-account-snapshot-cross-check):
    20.11.2026 после сбоя брокера портфель пришёл пустым при живом шорте Si, сверка заморозила Si,
    а хранитель стопа снял настоящий стоп. Противоречивый снимок — ``BrokerCallError`` с кодом
    ``INCONSISTENT``: дальше с ним поступают как с непрочитанным счётом.
    """
    names = {} if names is None else names
    try:
        margin = gateway.call("users.get_margin_attributes", account_id=account.account_id)
        liquid = money_to_decimal(margin.liquid_portfolio)
    except BrokerCallError:
        liquid = None  # не у каждого счёта есть маржинальные атрибуты; деньги берутся из позиций
    # Портфель и позиции — подряд, чтобы своё исполнение реже попадало между ними.
    portfolio = gateway.call("operations.get_portfolio", account_id=account.account_id)
    positions = gateway.call("operations.get_positions", account_id=account.account_id)
    conflicts = position_conflicts(portfolio, positions)
    if conflicts:
        log.warning("Снимок счёта противоречив, сверка его не принимает: %s", "; ".join(
            f"{short_name_by_uid(gateway, uid, names)} — портфель {ours}, позиции {theirs}"
            for uid, ours, theirs in conflicts))
        raise BrokerCallError("operations.get_portfolio", INCONSISTENT,
                              message="портфель и позиции брокера расходятся")
    holdings = tuple((short_name_by_uid(gateway, uid, names), lots) for uid, lots, _ in portfolio_positions(portfolio))
    orders = tuple(short_name_by_uid(gateway, order.instrument_uid, names) for order in active_orders(gateway, account))
    stops = tuple(short_name_by_uid(gateway, stop.instrument_uid, names)
                  for stop in active_stop_orders(gateway, account))
    return AccountSnapshot(rub_of(positions), liquid, holdings, orders, stops, names)


def position_conflicts(portfolio, positions) -> list[tuple[str, int, int]]:
    """Инструменты, где ``GetPortfolio`` и ``GetPositions`` расходятся: ``(uid, портфель, позиции)``.

    Фьючерс сравнивается точно: лот фьючерса — один контракт, ``balance + blocked`` — контракты.
    У бумаг ``GetPositions`` считает штуки, портфель — лоты, поэтому сравниваются только знак и
    наличие позиции.
    """
    futures = {item.instrument_uid: item.balance + item.blocked for item in _listed(positions, "futures")}
    others = {item.instrument_uid: item.balance + item.blocked
              for item in (*_listed(positions, "securities"), *_listed(positions, "options"))}
    ours: dict[str, int] = {}
    exact: set[str] = set(futures)
    for item in portfolio.positions:
        if item.instrument_type == "currency":
            continue
        ours[item.instrument_uid] = ours.get(item.instrument_uid, 0) + int(quotation_to_decimal(item.quantity_lots))
        if item.instrument_type == "futures":
            exact.add(item.instrument_uid)
    conflicts = []
    for uid in sorted(set(ours) | set(futures) | set(others)):
        mine = ours.get(uid, 0)
        if uid in exact:
            theirs = futures.get(uid, 0)
            if mine != theirs:
                conflicts.append((uid, mine, theirs))
        else:
            theirs = others.get(uid, 0)
            if _sign(mine) != _sign(theirs):
                conflicts.append((uid, mine, theirs))
    return conflicts


def _listed(response, name: str) -> list:
    """Список из ответа SDK; незаданное поле у SDK — заглушка ``object()``, не список."""
    value = getattr(response, name, None)
    return list(value) if isinstance(value, (list, tuple)) else []


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


# --- правило счёта песочницы (add-sandbox-contour) ----------------------------
ROBOT_ACCOUNT_NAME = "My_roBot"


class AccountConflict(AccountRuleError):
    """Счетов песочницы с именем робота больше одного: какой закрыть, решает человек."""

    def __init__(self, masked: list[str]):
        super().__init__(masked)
        self.masked = masked

    def __str__(self) -> str:
        return (f"Счетов песочницы {ROBOT_ACCOUNT_NAME} несколько: {', '.join(self.masked)}. "
                "Оставьте один, остальные закройте.")


def robot_account(gateway) -> RobotAccount:
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


# --- правило счёта «Счёта» (add-account-contour, design.md решение 3) -----------
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
