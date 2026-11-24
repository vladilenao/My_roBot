"""Порт брокера контура на брокере (refactor-broker-port): общие типы, отчёты и ошибки.

Общий код контура (``broker``, ``stops``, ``process`` …) знает брокера только через этот модуль.
SDK брокера и пакеты адаптеров (``tinvest``, на Ф2–Ф3 ``finam``) сюда не импортируются.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


# --- ошибки -----------------------------------------------------------------
class BrokerCallError(Exception):
    """Отказ брокера или сбой сети: код gRPC и описание брокера."""

    def __init__(self, method: str, code: str, details: str = "", message: str = ""):
        super().__init__(method, code, details, message)
        self.method = method
        self.code = code
        self.details = details
        self.message = message

    def __str__(self) -> str:
        head = " ".join(part for part in (self.code, self.details) if part)
        return f"{head}: {self.message}" if self.message else head


class OrderFailure(Exception):
    """Отказ вызова заявки: ``unknown`` — судьба неизвестна, ``duplicate`` — ключ уже был,
    ``rate_limited`` — повторы шлюза исчерпаны, ``not_found`` — по ключу пусто, ``rejected`` — отказ брокера."""

    def __init__(self, kind: str, code: str = "", details: str = "", message: str = ""):
        super().__init__(kind, code, details, message)
        self.kind, self.code, self.details, self.message = kind, code, details, message

    def __str__(self) -> str:
        return " ".join(part for part in (self.code, self.details, self.message) if part) or self.kind

    @property
    def reason(self) -> str:
        """Причина отказа для журнала: ``broker:<код>``."""
        return f"broker:{self.details or self.code or self.kind}"


class AccountRuleError(Exception):
    """Счёт робота не определён правилом вида (у «Счёта» — номер из окружения, у песочницы — имя)."""


# --- счёт -------------------------------------------------------------------
def mask_account(account_id: str) -> str:
    """Номер счёта для человека: только последние четыре символа."""
    return f"…{str(account_id)[-4:]}"


@dataclass(frozen=True)
class RobotAccount:
    account_id: str
    opened: bool
    access_level: str | None = None   # права токена на счёт брокера (add-account-contour); у песочницы нет

    @property
    def masked(self) -> str:
        return mask_account(self.account_id)


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


# --- отчёты брокера: цены в пунктах -------------------------------------------
@dataclass(frozen=True)
class BrokerTrade:
    """Сделка биржи по заявке: номер, цена в пунктах, количество (как прислал брокер), время."""

    trade_id: str
    price: Decimal
    quantity: int
    at: datetime


@dataclass(frozen=True)
class OrderReport:
    """Состояние заявки у брокера: ответ на постановку, ``order_state`` или стрим."""

    broker_order_id: str
    key: str | None
    status: str                        # new · partial · fill · cancelled · rejected · unknown
    lots_requested: int
    lots_executed: int
    instrument_uid: str = ""
    trades: tuple[BrokerTrade, ...] = ()
    temporary: bool = False               # номер заявки временный (первое событие стрима Т-Инвест)
    at: datetime | None = None
    executed_fee: Decimal | None = None   # комиссия заявки, если брокер её отдаёт; нет — ``None``
    average: Decimal | None = None        # средняя исполнения в пунктах, когда сделок биржи в отчёте нет

    @property
    def terminal(self) -> bool:
        return self.status in {"fill", "cancelled", "rejected"}


@dataclass(frozen=True)
class StopReport:
    """Стоп-заявка у брокера."""

    stop_order_id: str
    instrument_uid: str
    side: str                     # BUY · SELL
    lots: int
    status: str                   # active · executed · cancelled · expired · unknown
    exchange_order_id: str = ""   # заявка, порождённая сработавшей стоп-заявкой
    created_at: datetime | None = None


@dataclass(frozen=True)
class StopQuery:
    """Строка журнала стоп-заявок, которую контур ищет у брокера (``StopPort.poll_stops``).

    ``search`` — номер мог не прийти (ответ на постановку потерян): адаптер ищет стоп-заявку своим способом;
    ``lost`` — строка «потеряна»: адаптер ищет лишнюю стоп-заявку, номер строки не смотрит.
    """

    key: str
    stop_order_id: str
    instrument_uid: str
    side: str
    lots: int
    stop_price: Decimal           # пункты
    sent_at: datetime
    search: bool = False
    lost: bool = False


@dataclass(frozen=True)
class StopMatch:
    """Ответ на ``StopQuery``: стоп-заявка брокера или ``None``; ``candidates`` — сколько подходящих нашёл поиск."""

    report: StopReport | None
    candidates: int = 0
    searched: bool = False


# --- порты --------------------------------------------------------------------
@runtime_checkable
class OrderPort(Protocol):
    """Заявки: постановка, снятие, состояние. Отказ — ``OrderFailure``."""

    def post_limit(self, uid: str, side: str, lots: int, price: Decimal, key: str) -> OrderReport: ...

    def post_market(self, uid: str, side: str, lots: int, key: str) -> OrderReport: ...

    def cancel(self, broker_order_id: str) -> None: ...

    def order_state(self, uid: str, key: str | None, number: str | None) -> OrderReport | None:
        """Заявка по нашему ключу или номеру брокера (как искать — решает адаптер); ``None`` — не найдена."""
        ...


@runtime_checkable
class StopPort(Protocol):
    """Стоп-заявки: постановка, снятие, поиск."""

    def post_stop(self, uid: str, side: str, lots: int, stop_price: Decimal, key: str) -> str: ...

    def cancel_stop(self, stop_order_id: str) -> None: ...

    def poll_stops(self, queries: list[StopQuery], known: set[str], since: datetime,
                   until: datetime) -> list[StopMatch]:
        """Ответ на каждый запрос по порядку; ``known`` — номера стоп-заявок, уже привязанных к журналу."""
        ...


@runtime_checkable
class MarketPort(Protocol):
    """Цены, коридор, номер инструмента и ГО у брокера."""

    def last_price(self, uid: str) -> Decimal | None: ...

    def price_band(self, uid: str) -> tuple[Decimal, Decimal] | None: ...

    def instrument_uid(self, ticker: str, future: bool) -> str: ...

    def futures_margin(self, uid: str) -> dict: ...


@runtime_checkable
class OrderFeed(Protocol):
    """Стрим заявок счёта: отчёты, ``None`` — соединение живо."""

    def order_stream(self) -> Iterator[OrderReport | None]: ...


@runtime_checkable
class MarginLimits(Protocol):
    """Необязательная возможность: сколько лотов брокер даёт по обеспечению счёта."""

    def max_lots(self, uid: str, side: str, price: Decimal) -> int: ...


@runtime_checkable
class FeeSource(Protocol):
    """Необязательная возможность: настоящие комиссии ждущих исполнений журнала сделок."""

    def run(self, now: datetime) -> list: ...


class BrokerApi(OrderPort, StopPort, MarketPort, OrderFeed, Protocol):
    """Все порты торговли — для удобства сборки; потребители объявляют свой узкий порт."""


@dataclass(frozen=True)
class TradingLink:
    """Торговая связка адаптера для счёта робота: порты, данные брокера, источник комиссий."""

    api: BrokerApi
    margin_limits: MarginLimits | None
    fills_prefix: str                       # номера исполнений <префикс>:<сделка>, <префикс>-order:<заявка>:<лоты>
    margin_every: timedelta                 # как часто перечитывать ГО брокера
    fee_source: Callable[[Callable[[], Any]], FeeSource] | None = None


@runtime_checkable
class BrokerSession(Protocol):
    """Брокер на открытом соединении: правило счёта, допуск, снимок, капитал, имена, торговля."""

    def find_account(self) -> RobotAccount:
        """Счёт робота; не определён — ``AccountRuleError``, сбой брокера — ``BrokerCallError``."""
        ...

    def read_facts(self, account: RobotAccount) -> dict | None:
        """Факты допуска (права, допуски, лимиты); отказ — ``AdmissionError``; ``None`` — у вида их нет."""
        ...

    def read_account(self, account: RobotAccount, names: dict | None = None) -> AccountSnapshot: ...

    def capital(self, account: RobotAccount) -> Decimal: ...

    def short_name(self, uid: str, names: dict) -> str: ...

    def trading(self, account: RobotAccount, book) -> TradingLink: ...

    def fee_probes(self, path) -> Any:
        """Разведка комиссий на чтение или ``None``."""
        ...
