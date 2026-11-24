"""Сессия Финама контура на брокере (add-finam-contour, решения 3 и 5).

``FinamSession`` — ``BrokerSession`` только на чтение: правило счёта, факты допуска, снимок. Торговой
связки у неё нет (Ф3): контур с ``orders = true`` на ней не собирается. ``adapter`` собирает фабрику сессий
для точки сборки ``run.py``; каталог карточек ядра она получает оттуда же.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, contextmanager

from src.broker_contour.finam.account import account_rule, read_account
from src.broker_contour.finam.admission import read_facts
from src.broker_contour.finam.symbols import FinamSymbols
from src.broker_contour.finam.transport import FinamTransport
from src.broker_contour.port import AccountSnapshot, BrokerCallError, RobotAccount


class FinamSession:
    def __init__(self, transport, symbols: FinamSymbols, account_id: str, *, orders: bool = False):
        self.transport, self.symbols = transport, symbols
        self._account_id, self._orders = account_id, orders

    def find_account(self) -> RobotAccount:
        return account_rule(self.transport, self._account_id)

    def read_facts(self, account: RobotAccount) -> dict:
        return read_facts(self.transport, account, orders=self._orders)

    def read_account(self, account: RobotAccount, names: dict | None = None) -> AccountSnapshot:
        return read_account(self.transport, self.symbols, account, names)

    def fee_probes(self, path):
        return None                         # разведка комиссий на чужих сделках — только у Т-Инвест


def adapter(*, secret: str | None, account_id: str | None, catalog_factory: Callable, orders: bool = False,
            transport_factory: Callable = FinamTransport) -> Callable:
    """Фабрика сессий Финама: транспорт на секрете и каталог карточек ядра от точки сборки."""

    @contextmanager
    def open_session():
        with ExitStack() as stack:
            try:
                catalog = stack.enter_context(catalog_factory())
            except ValueError as exc:        # нет токена справочника ядра: допуск откажет с тревогой
                raise BrokerCallError("catalog", "NO_CATALOG", str(exc)) from None
            transport = stack.enter_context(transport_factory(secret, account_id=account_id))
            yield FinamSession(transport, FinamSymbols(transport, catalog, account_id or ""), account_id or "",
                               orders=orders)

    return open_session
