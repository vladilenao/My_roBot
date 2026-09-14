import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.trade_journal import JournalState, RestoredOrder, RestoredPosition

UTC = timezone.utc


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    NONE = "NONE"  # событие без привязки к ордеру (клиринговый снимок)


@dataclass(frozen=True)
class ContractMeta:
    """Биржевые параметры контракта (фьючерс): шаг цены, стоимость шага, ГО."""

    ticker: str
    price_step: float
    step_cost: float
    go_buy: float
    go_sell: float
    currency: str = "RUB"

    def position_value(self, qty: int, price: float) -> float:
        """Номинальная стоимость позиции в валюте: qty * шагов * стоимость шага."""
        if self.price_step <= 0:
            return 0.0
        return qty * (price / self.price_step) * self.step_cost


@dataclass
class Position:
    """Открытая позиция. Мутабельна и обновляется исполнителем (JournalBroker)."""

    position_id: str
    ticker: str
    side: str
    qty: int
    avg_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    ts_entry: Optional[datetime]
    over_risk: bool = False
    timeframe: str = ""
    protective: Optional["ProtectiveOrder"] = field(default=None, repr=False)

    def mark_over_risk(self) -> None:
        if not self.over_risk:
            self.over_risk = True

    def average(self, price: float, add_qty: int) -> None:
        total = self.qty * self.avg_price + add_qty * price
        self.qty += add_qty
        self.avg_price = round(total / self.qty, 6)

    def reduce(self, close_qty: int) -> None:
        self.qty = max(0, self.qty - close_qty)


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
    """Входное предложение позиционирования (сделка на открытие/добавление).

    `source` сохраняет происхождение сигнала (стратегия/имя) для журнала и
    уведомлений; в продакшене строится адаптером исполнения из `Decision`.
    """

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
    reason: Optional[str] = None  # причина отказа/ограничения для события


class Account:
    """Счёт трейдера. Баланс = стартовый депозит + реализованная прибыль;

    плюс плавающий P/L открытых позиций (mark-to-market по последней цене).
    """

    def __init__(self, initial_deposit: float):
        self.initial_deposit = float(initial_deposit)
        self.realized_total = 0.0  # кумулятивная реализованная прибыль
        self.realized_cycle = 0.0  # с последнего клиринга (сбрасывается)
        self.floating = 0.0        # плавающий P/L открытых позиций

    @property
    def balance(self) -> float:
        return self.initial_deposit + self.realized_total

    @property
    def equity(self) -> float:
        """Средства с учётом плавающей прибыли открытых позиций."""
        return self.balance + self.floating

    def realize(self, pnl: float) -> None:
        self.realized_cycle += pnl
        self.realized_total += pnl

    def set_floating_pnl(self, pnl: float) -> None:
        self.floating = float(pnl)

    def clear(self) -> None:
        self.realized_cycle = 0.0
        self.floating = 0.0

    def snapshot(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "realized_cycle": round(self.realized_cycle, 2),
        }


class PositionManager:
    """Портфельное позиционирование: риск-бюджет, размер позиции, over_risk.

    Не зависит от исполнителя: получает восстановленное состояние журнала и
    контрактные метаданные, возвращает решения по размерам и события лимита.
    """

    def __init__(
        self,
        state: JournalState | None,
        initial_deposit: float,
        max_risk_pct: float,
    ):
        self.account = Account(initial_deposit)
        self.max_risk_pct = float(max_risk_pct)
        self.positions: dict[str, Position] = {}
        self.pending: dict[int, RestoredOrder] = {}
        if state is not None:
            self.account.realize(state.realized)
            self.pending = dict(state.orders)
            for pos in state.positions.values():
                self.positions[pos.position_id] = Position(
                    position_id=pos.position_id,
                    ticker=pos.ticker,
                    side=pos.side,
                    qty=pos.qty,
                    avg_price=pos.avg_price,
                    stop_price=pos.stop_price or None,
                    take_profit=pos.take_profit,
                    ts_entry=pos.ts_entry,
                    over_risk=pos.over_risk,
                    timeframe=pos.timeframe,
                )

    def budget(self) -> float:
        """Риск-бюджет: equity * max_risk_pct / 100 (учёт плавающего P/L)."""
        return self.account.equity * self.max_risk_pct / 100.0

    def has_open_for(self, ticker: str) -> bool:
        return any(p.ticker == ticker and p.qty > 0 for p in self.positions.values())

    def get_go(self, qty: int, contract: ContractMeta, side: str) -> float:
        """Требуемое гарантийное обеспечение (в руб) для заявки на стороне `side`."""
        side_go = contract.go_buy if side == "BUY" else contract.go_sell
        return side_go * qty

    def evaluate_signals(
        self,
        signals: list[Signal],
        contracts: dict[str, ContractMeta] | None = None,
    ) -> list[SizingOutcome]:
        """FIFO: по каждому сигналу в порядке поступления сохраняется очередь."""
        contracts = contracts or {}
        outcomes: list[SizingOutcome] = []
        for signal in signals:
            contract = contracts.get(signal.ticker)
            outcomes.append(
                self._size_signal(signal, contract)
                if contract is not None
                else SizingOutcome(
                    signal=signal,
                    qty=0,
                    risk_rub=0.0,
                    risk_pct=signal.risk_pct,
                    reason="no-contract-meta",
                )
            )
        return outcomes

    def _size_signal(self, signal: Signal, contract: ContractMeta) -> SizingOutcome:
        if self.has_open_for(signal.ticker):
            return SizingOutcome(
                signal=signal, qty=0, risk_rub=0.0,
                risk_pct=signal.risk_pct, reason="otherexisting",
            )
        if signal.stop_distance_pct is None or signal.stop_distance_pct <= 0:
            return SizingOutcome(
                signal=signal, qty=0, risk_rub=0.0,
                risk_pct=signal.risk_pct, reason="stop-missing",
            )
        used = min(signal.risk_pct, self.max_risk_pct)
        if used <= 0:
            return SizingOutcome(
                signal=signal, qty=0, risk_rub=0.0,
                risk_pct=signal.risk_pct, reason="risk-pct-exceeded",
            )
        risk_rub = self.account.equity * used / 100.0
        stop_distance = (signal.stop_distance_pct / 100.0) * signal.entry_price
        if contract.price_step <= 0 or contract.step_cost <= 0 or stop_distance <= 0:
            return SizingOutcome(
                signal=signal, qty=0, risk_rub=risk_rub,
                risk_pct=used, reason="no-margin-math",
            )
        qty = math.floor(risk_rub / (stop_distance / contract.price_step * contract.step_cost))
        if qty < 1:
            return SizingOutcome(
                signal=signal, qty=0, risk_rub=risk_rub,
                risk_pct=used, reason="qty-negative",
            )
        return SizingOutcome(
            signal=signal, qty=int(qty), risk_rub=risk_rub,
            risk_pct=used, reason=None,
        )

    def margin_ok(self, qty: int, contract: ContractMeta, side: str) -> bool:
        """Соответствие заявки ГО: используемое ГО <= баланс."""
        return self.account.balance >= self.get_go(qty, contract, side)

    def over_risk_cap(self) -> float:
        """Порог «перекоса»: номинальная стоимость позиции (3 × стартовый депозит)."""
        return 3.0 * self.account.initial_deposit

    def apply_over_risk(self, positions: dict[str, Position]) -> list[str]:
        """Отмечает позиции, чья стоимость превысила порог; возвращает их id."""
        flagged = []
        for pos in positions.values():
            if pos.qty <= 0 or pos.ticker not in self._prices:
                continue
            contract = self._contracts.get(pos.ticker)
            if contract is None:
                continue
            value = contract.position_value(pos.qty, self._prices[pos.ticker])
            if value > self.over_risk_cap():
                pos.mark_over_risk()
                flagged.append(pos.position_id)
        return flagged

    def track_bar(self, prices: dict[str, float], contracts: dict[str, ContractMeta] | None = None) -> list[str]:
        """Обновление котировок и проверка лимита перекоса: возврат position_id заявок,
        подлежащих отмене (сначала самая старая из-за FIFO, не принадлежащая ove_risk-позициям)."""
        self._prices = dict(prices)
        self._contracts = dict(contracts or {})
        over_ids = set(self.apply_over_risk(self.positions))
        if not over_ids:
            return []
        cancel_candidates = sorted(
            (o for o in self.pending.values() if o.position_id not in over_ids),
            key=lambda o: (o.ts_order, o.order_id),
        )
        return [cancel_candidates[0].position_id] if cancel_candidates else []

    def register_order(self, order_id: int, restored: RestoredOrder) -> None:
        self.pending[order_id] = restored

    def drop_order(self, order_id: int) -> None:
        self.pending.pop(order_id, None)

    def drop_order_stale(self, position_id: str) -> None:
        """Убирает отложенные заявки закрывшейся позиции (защитные, entry)."""
        self.pending = {oid: o for oid, o in self.pending.items() if o.position_id != position_id}