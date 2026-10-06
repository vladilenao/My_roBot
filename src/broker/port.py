from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from src.trade_management.actions import TradeAction


class ExecutionStatus(StrEnum):
    FILL = "fill"
    PARTIAL = "partial"
    ACK = "ack"
    REJECT = "reject"
    CANCEL = "cancel"


class FeeSource(StrEnum):
    BROKER = "broker"
    CONFIGURED = "configured"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutionEvent:
    """A broker outcome for one addressed trade command."""

    execution_id: str
    order_id: str | None
    command_id: str
    trade_id: str
    status: ExecutionStatus
    filled_quantity: int
    price: Decimal | None
    fee: Decimal | None
    timestamp: datetime
    reason: str
    market_low: Decimal | None = None
    market_high: Decimal | None = None
    fee_source: FeeSource = FeeSource.BROKER
    reference_price: Decimal | None = None
    reference_kind: str | None = None
    order_side: str | None = None
    slippage_amount: Decimal | None = None
    slippage_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fee_source", FeeSource(self.fee_source))
        if not all((self.execution_id, self.command_id, self.trade_id, self.reason)):
            raise ValueError("execution, command, trade identifiers and reason are required")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity cannot be negative")
        if self.fee is not None and (not self.fee.is_finite() or self.fee < 0):
            raise ValueError("fee cannot be negative")
        is_fill = self.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}
        if is_fill and (self.filled_quantity == 0 or self.price is None):
            raise ValueError("fill and partial events require quantity and price")
        if not is_fill and self.filled_quantity != 0:
            raise ValueError("only fill and partial events can carry a filled quantity")
        if self.order_side not in {None, "BUY", "SELL"}:
            raise ValueError("invalid order_side")
        if self.reference_price is not None and (not self.reference_price.is_finite() or self.reference_price <= 0):
            raise ValueError("reference_price must be finite and positive")


def resolve_execution_fee(event: ExecutionEvent, commission: Decimal | None) -> ExecutionEvent:
    """Нулевой broker факт сохраняется; отсутствующая сумма оценивается по snapshot."""
    if event.fee is not None:
        return event
    if event.status not in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL} or commission is None:
        return replace(event, fee=Decimal(0), fee_source=FeeSource.UNKNOWN)
    return replace(event, fee=commission * event.filled_quantity, fee_source=FeeSource.CONFIGURED)


@dataclass(frozen=True)
class FeeAdjustment:
    adjustment_id: str
    execution_id: str
    new_fee: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.adjustment_id or not self.execution_id or not self.new_fee.is_finite() or self.new_fee < 0:
            raise ValueError("fee adjustment requires identifiers and finite nonnegative new_fee")


class BrokerPort(ABC):
    """Broker contract for addressable trade actions and their outcomes."""

    @abstractmethod
    def submit(self, action: TradeAction, now: datetime) -> ExecutionEvent:
        """Submit one addressable action and return its broker outcome."""
