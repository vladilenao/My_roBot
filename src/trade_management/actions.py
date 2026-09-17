from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TradeAction:
    command_id: str
    trade_id: str
    state_revision: int
    reason: str

    def __post_init__(self) -> None:
        if not self.command_id or not self.trade_id:
            raise ValueError("command_id and trade_id are required")
        if self.state_revision < 0:
            raise ValueError("state_revision cannot be negative")
        if not self.reason:
            raise ValueError("reason is required")


@dataclass(frozen=True)
class OpenTrade(TradeAction):
    quantity: int


@dataclass(frozen=True)
class AddToTrade(TradeAction):
    quantity: int


@dataclass(frozen=True)
class ReduceTrade(TradeAction):
    quantity: int
    target_id: str | None = None


@dataclass(frozen=True)
class CloseTrade(TradeAction):
    pass


@dataclass(frozen=True)
class MoveStop(TradeAction):
    stop_price: Decimal


@dataclass(frozen=True)
class CancelEntry(TradeAction):
    pass
