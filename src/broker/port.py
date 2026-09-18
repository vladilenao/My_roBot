from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    fee: Decimal
    timestamp: datetime
    reason: str
    market_low: Decimal | None = None
    market_high: Decimal | None = None

    def __post_init__(self) -> None:
        if not all((self.execution_id, self.command_id, self.trade_id, self.reason)):
            raise ValueError("execution, command, trade identifiers and reason are required")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity cannot be negative")
        if self.fee < 0:
            raise ValueError("fee cannot be negative")
        is_fill = self.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}
        if is_fill and (self.filled_quantity == 0 or self.price is None):
            raise ValueError("fill and partial events require quantity and price")
        if not is_fill and self.filled_quantity != 0:
            raise ValueError("only fill and partial events can carry a filled quantity")


class BrokerPort(ABC):
    """Broker contract for addressable trade actions and their outcomes."""

    @abstractmethod
    def submit(self, action: TradeAction, now: datetime) -> ExecutionEvent:
        """Submit one addressable action and return its broker outcome."""
