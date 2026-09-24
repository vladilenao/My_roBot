import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    NONE = "NONE"  # событие без привязки к ордеру (клиринговый снимок)


@dataclass(frozen=True)
class ContractMeta:
    """Биржевые параметры контракта (фьючерс): шаг цены, стоимость шага, ГО.

    ``expiration_date`` — naive UTC момент экспирации фьючерса; для контрактов
    без даты экспирации (акции) остаётся ``None``.
    """

    ticker: str
    price_step: float
    step_cost: float
    go_buy: float
    go_sell: float
    currency: str = "RUB"
    expiration_date: Optional[datetime] = None

    def position_value(self, qty: int, price: float) -> float:
        """Номинальная стоимость позиции в валюте: qty * шагов * стоимость шага."""
        if self.price_step <= 0:
            return 0.0
        return qty * (price / self.price_step) * self.step_cost


@dataclass
class Position:
    """Фактическая позиция, изменяемая только подтвержденными исполнениями."""

    position_id: str
    ticker: str
    side: str
    _qty: int
    _avg_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    ts_entry: Optional[datetime]
    over_risk: bool = False
    timeframe: str = ""
    protective: Optional["ProtectiveOrder"] = field(default=None, repr=False)

    def __init__(
        self,
        position_id: str,
        ticker: str,
        side: str,
        qty: int,
        avg_price: float,
        stop_price: Optional[float],
        take_profit: Optional[float],
        ts_entry: Optional[datetime],
        over_risk: bool = False,
        timeframe: str = "",
        protective: Optional["ProtectiveOrder"] = None,
    ):
        self.position_id = position_id
        self.ticker = ticker
        self.side = side
        self._qty = self._validate_qty(qty)
        self._avg_price = self._validate_price(avg_price)
        self.stop_price = stop_price
        self.take_profit = take_profit
        self.ts_entry = ts_entry
        self.over_risk = over_risk
        self.timeframe = timeframe
        self.protective = protective

    @property
    def qty(self) -> int:
        return self._qty

    @property
    def avg_price(self) -> float:
        return self._avg_price

    def apply_fill(self, price: float, qty: int) -> None:
        """Apply a confirmed increasing fill and recompute the weighted average."""
        fill_qty = self._validate_qty(qty)
        fill_price = self._validate_price(price)
        total_qty = self._qty + fill_qty
        self._avg_price = round((self._qty * self._avg_price + fill_qty * fill_price) / total_qty, 6)
        self._qty = total_qty

    def reduce(self, qty: int) -> None:
        """Apply a confirmed reducing fill without changing the entry average."""
        close_qty = self._validate_qty(qty)
        if close_qty > self._qty:
            raise ValueError("close quantity exceeds position quantity")
        self._qty -= close_qty

    def mark_over_risk(self) -> None:
        if not self.over_risk:
            self.over_risk = True

    @staticmethod
    def _validate_qty(qty: int) -> int:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError("position quantity must be a positive integer")
        return qty

    @staticmethod
    def _validate_price(price: float) -> float:
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("position price must be finite and positive")
        return value


@dataclass(frozen=True)
class ProtectiveOrder:
    """Одновременно заявка на стоп-лосс и тейк-профит позиции (стоп приоритетен)."""

    position_id: str
    ticker: str
    side: str
    qty: int
    stop_price: Optional[float]
    take_profit: Optional[float]
    ts_created: datetime


@dataclass(frozen=True)
class BrokerEvent:
    """Событие исполнителя (заявка, сделка, отмена, снимок) для уведомлений."""

    type: str
    ts: datetime
    position_id: str
    message: str


@dataclass(frozen=True)
class OrderResult:
    """Результат операции исполнителя без привязки к движению рынка."""

    order_id: int
    status: OrderStatus
    position_id: str
    side: str
    qty: int
    limit_price: Optional[float]
    stop_price: Optional[float]
    take_profit: Optional[float]
    reason: str
    message: str
    ts_order: datetime
    events: tuple[BrokerEvent, ...] = ()


@dataclass(frozen=True)
class Signal:
    """Входное предложение позиционирования (сделка на открытие/добавление)."""

    position_id: str
    ticker: str
    side: str
    entry_price: float
    stop_price: Optional[float]
    stop_distance_pct: Optional[float]
    risk_pct: float
    take_profit: Optional[float]
    timeframe: str
    source: str = ""
    risk_rub: float = 0.0
    qty: int = 0


@dataclass(frozen=True)
class SizingOutcome:
    """Результат расчёта размера позиции. `qty == 0` означает отказ разместить."""

    signal: Signal
    qty: int
    risk_rub: float
    risk_pct: float
    reason: Optional[str] = None


@dataclass(frozen=True)
class PendingOrder:
    """Минимальное доменное представление ожидающего legacy-ордера."""

    order_id: int
    position_id: str
    ticker: str
    side: str
    qty: int
    limit_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    timeframe: str
    ts_order: datetime
    risk_pct: Optional[float]
    risk_rub: Optional[float]
    ttl: Optional[int]
