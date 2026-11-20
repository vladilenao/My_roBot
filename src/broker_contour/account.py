"""Снимок счёта брокера: деньги, позиции, активные заявки и стоп-заявки коротким именем."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from t_tech.invest.utils import money_to_decimal, quotation_to_decimal

from src.logging_setup import get_logger
from src.sandbox.account import RobotAccount
from src.sandbox.actions import active_orders, active_stop_orders, portfolio_positions, rub_of
from src.sandbox.gateway import BrokerCallError
from src.sandbox.instruments import short_name_by_uid

log = get_logger(__name__)

# Код «снимок противоречив» (fix-account-snapshot-cross-check): портфель и позиции брокера расходятся.
INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True)
class AccountSnapshot:
    rub: Decimal
    liquid_portfolio: Decimal | None
    holdings: tuple[tuple[str, int], ...] = ()       # (короткое имя, лоты)
    orders: tuple[str, ...] = ()                      # короткое имя на каждую активную заявку
    stop_orders: tuple[str, ...] = ()                 # короткое имя на каждую активную стоп-заявку
    names: dict = field(default_factory=dict, compare=False)

    def payload(self) -> dict:
        """Поля ``status.json`` и события ``started``."""
        return {
            "capital": {"rub": _kopecks(self.rub),
                        "liquid_portfolio": None if self.liquid_portfolio is None else _kopecks(self.liquid_portfolio)},
            "positions": [{"name": name, "lots": lots} for name, lots in self.holdings],
            "orders": len(self.orders),
            "stop_orders": len(self.stop_orders),
        }


def _kopecks(value: Decimal) -> str:
    """Деньги брокера до копейки: у песочницы бывают доли копейки после пополнений и комиссий."""
    return format(value.quantize(Decimal("0.01")), "f")


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
