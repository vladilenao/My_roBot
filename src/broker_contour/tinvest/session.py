"""Сессия Т-Инвест контура на брокере (refactor-broker-port, design.md решения 3–4).

``TInvestSession`` — ``BrokerSession`` на открытом шлюзе: правило счёта, факты допуска, снимок,
капитал, имена и торговая связка. ``adapter`` собирает фабрику сессий для песочницы и «Счёта»;
её вызывает только точка сборки ``run.py`` (и тесты).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from src.broker_contour.port import AccountSnapshot, RobotAccount, TradingLink
from src.broker_contour.prices import PriceBook
from src.broker_contour.tinvest.account import account_by_id, read_account, robot_account
from src.broker_contour.tinvest.admission import read_facts
from src.broker_contour.tinvest.api import TInvestApi
from src.broker_contour.tinvest.fees import FILLS_PREFIX, FeeSync
from src.broker_contour.tinvest.gateway import AccountGateway, SandboxGateway
from src.broker_contour.tinvest.port import TInvestPort
from src.sandbox.actions import rub_balance
from src.sandbox.instruments import short_name_by_uid

MARGIN_EVERY = timedelta(days=1)         # ГО фьючерсов из GetFuturesMargin: при допуске инструмента и раз в сутки
SANDBOX, ACCOUNT = "sandbox", "account"


class _FeeProbes:
    """Разведка комиссий «Счёта» на чтение (``src.account.fee_probes``) на шлюзе сессии."""

    def __init__(self, probes, gateway):
        self._probes, self._gateway = probes, gateway

    def observe(self, now: datetime, snapshot: AccountSnapshot) -> None:
        self._probes.observe(now, snapshot)

    def run(self, account: RobotAccount, now: datetime) -> None:
        self._probes.run(self._gateway, account.account_id, now)


class TInvestSession:
    def __init__(self, gateway, *, rule: Callable, facts: Callable | None = None, margin_limits: bool = False,
                 fee_probes: Callable | None = None):
        self.gateway = gateway
        self._rule = rule
        self._facts = facts
        self._margin_limits = margin_limits
        self._fee_probes = fee_probes

    def find_account(self) -> RobotAccount:
        return self._rule(self.gateway)

    def read_facts(self, account: RobotAccount) -> dict | None:
        return None if self._facts is None else self._facts(self.gateway, account)

    def read_account(self, account: RobotAccount, names: dict | None = None) -> AccountSnapshot:
        return read_account(self.gateway, account, names)

    def capital(self, account: RobotAccount):
        return rub_balance(self.gateway, account)

    def short_name(self, uid: str, names: dict) -> str:
        return short_name_by_uid(self.gateway, uid, names)

    def trading(self, account: RobotAccount, book: PriceBook) -> TradingLink:
        api = TInvestApi(self.gateway, account.account_id)
        port = TInvestPort(api, book)
        return TradingLink(api=port, margin_limits=port if self._margin_limits else None, fills_prefix=FILLS_PREFIX,
                           margin_every=MARGIN_EVERY, fee_source=lambda connection: FeeSync(api, connection))

    def fee_probes(self, path: Path):
        return None if self._fee_probes is None else _FeeProbes(self._fee_probes(path), self.gateway)


def adapter(kind: str, *, token: str | None, account_id: str | None = None, orders: bool = False,
            gateway_factory: Callable | None = None, rule: Callable | None = None, facts: Callable | None = None,
            fee_probes: Callable | None = None) -> Callable:
    """Фабрика сессий Т-Инвест для контура вида ``sandbox`` или ``account``.

    Песочница: шлюз песочницы, счёт по имени ``My_roBot``, без фактов допуска и ``GetMaxLots``.
    «Счёт»: шлюз счёта, счёт по номеру ``account_id``, факты допуска по ``orders``, ``GetMaxLots``.
    ``gateway_factory``, ``rule`` и ``facts`` заменяют значения вида (тесты); ``fee_probes`` — разведка комиссий.
    """
    if kind == SANDBOX:
        gateway_factory = gateway_factory or SandboxGateway
        rule = rule or robot_account
        margin_limits = False
    elif kind == ACCOUNT:
        gateway_factory = gateway_factory or AccountGateway
        rule = rule or (lambda gateway: account_by_id(gateway, account_id or ""))
        facts = facts or partial(read_facts, orders=orders)
        margin_limits = True
    else:
        raise ValueError(f"Неизвестный вид контура Т-Инвест {kind!r}")

    @contextmanager
    def open_session():
        with gateway_factory(token) as gateway:
            yield TInvestSession(gateway, rule=rule, facts=facts, margin_limits=margin_limits,
                                 fee_probes=fee_probes)

    return open_session
