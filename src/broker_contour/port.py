"""Порт брокера контура на брокере (refactor-broker-port): общие типы, отчёты и ошибки.

Общий код контура (``broker``, ``stops``, ``process`` …) знает брокера только через этот модуль.
SDK брокера и пакеты адаптеров (``tinvest``, на Ф2–Ф3 ``finam``) сюда не импортируются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


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


# --- отчёты брокера -----------------------------------------------------------
@dataclass(frozen=True)
class BrokerTrade:
    """Сделка биржи по заявке: номер, цена, количество (как прислал брокер), время.

    ``in_rub`` — цена пришла суммой (``OrderStage`` в ``GetOrderState``): у фьючерса это рубли за
    контракт. Цена сделки стрима (``OrderTrade``) — котировка в пунктах.
    """

    trade_id: str
    price: Decimal
    quantity: int
    at: datetime
    in_rub: bool = False


@dataclass(frozen=True)
class OrderReport:
    """Состояние заявки у брокера: из ответа ``PostOrder``, ``GetOrderState`` или стрима."""

    broker_order_id: str
    key: str | None
    status: str                        # new · partial · fill · cancelled · rejected · unknown
    lots_requested: int
    lots_executed: int
    instrument_uid: str = ""
    trades: tuple[BrokerTrade, ...] = ()
    executed_rub: Decimal | None = None   # сумма исполнения ``executed_order_price`` (рубли у фьючерса)
    initial_rub: Decimal | None = None    # сумма заявки ``initial_order_price``: запрошенные лоты × цена лота
    temporary: bool = False               # первое событие стрима с временным номером заявки
    at: datetime | None = None
    executed_fee: Decimal | None = None   # ``executed_commission`` состояния заявки; ноль и нет поля — ``None``

    @property
    def terminal(self) -> bool:
        return self.status in {"fill", "cancelled", "rejected"}


@dataclass(frozen=True)
class StopReport:
    """Стоп-заявка у брокера из ``GetStopOrders``.

    ``stop_price`` — как прислал брокер: у фьючерса это может быть рубли за контракт, а не пункты
    (разведка П19); брокер контура сравнивает оба прочтения. Нашего ключа у стоп-заявки в ответе нет.
    """

    stop_order_id: str
    instrument_uid: str
    side: str                     # BUY · SELL
    lots: int
    stop_price: Decimal | None
    status: str                   # active · executed · cancelled · expired · unknown
    exchange_order_id: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class OperationReport:
    """Операция счёта из ``GetOperationsByCursor`` (add-account-vs-emulation-report, решение 6).

    ``kind`` — ``trade`` (покупка или продажа) или ``fee`` («комиссия брокера»); ``amount`` — модуль
    суммы операции; ``commission`` — модуль поля комиссии или ``None``, если поле не задано;
    ``trades`` — номера сделок биржи с количеством из ``trades_info``.
    """

    operation_id: str
    parent_id: str
    kind: str
    at: datetime | None
    amount: Decimal
    commission: Decimal | None
    trades: tuple[tuple[str, int], ...] = ()
