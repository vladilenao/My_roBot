"""Снимок счёта брокера: деньги, позиции, активные заявки и стоп-заявки коротким именем."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from t_tech.invest.utils import money_to_decimal

from src.sandbox.account import RobotAccount
from src.sandbox.actions import active_orders, active_stop_orders, open_positions, rub_balance
from src.sandbox.gateway import BrokerCallError
from src.sandbox.instruments import short_name_by_uid


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
    """Прочитать счёт целиком; сбой брокера — ``BrokerCallError`` наружу (сверка пропускается)."""
    names = {} if names is None else names
    try:
        margin = gateway.call("users.get_margin_attributes", account_id=account.account_id)
        liquid = money_to_decimal(margin.liquid_portfolio)
    except BrokerCallError:
        liquid = None  # не у каждого счёта есть маржинальные атрибуты; деньги берутся из позиций
    holdings = tuple((short_name_by_uid(gateway, uid, names), lots) for uid, lots, _ in open_positions(gateway, account))
    orders = tuple(short_name_by_uid(gateway, order.instrument_uid, names) for order in active_orders(gateway, account))
    stops = tuple(short_name_by_uid(gateway, stop.instrument_uid, names)
                  for stop in active_stop_orders(gateway, account))
    return AccountSnapshot(rub_balance(gateway, account), liquid, holdings, orders, stops, names)
