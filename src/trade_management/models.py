from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


def _freeze(value: object) -> object:
    """Detach profile snapshots from mutable configuration data."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


class TradePhase(StrEnum):
    PLANNED = "PLANNED"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    BUILDING = "BUILDING"
    REDUCING = "REDUCING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class TargetPlan:
    target_id: str
    price: Decimal
    share: Decimal

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("target_id is required")
        if self.price <= 0:
            raise ValueError("target price must be positive")
        if not Decimal("0") < self.share <= Decimal("1"):
            raise ValueError("target share must be in (0, 1]")


@dataclass(frozen=True)
class ProfileSnapshot:
    name: str
    version: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("profile name is required")
        if not self.version:
            raise ValueError("profile version is required")
        object.__setattr__(self, "parameters", _freeze(dict(self.parameters)))


@dataclass(frozen=True)
class TradePlan:
    trade_id: str
    assignment_id: str
    instrument_id: str
    side: str
    signal_id: str
    reference_entry: Decimal
    stop_price: Decimal
    targets: tuple[TargetPlan, ...]
    profile: ProfileSnapshot
    created_at: datetime

    def __post_init__(self) -> None:
        if not all((self.trade_id, self.assignment_id, self.instrument_id, self.signal_id)):
            raise ValueError("trade, assignment, instrument, and signal identifiers are required")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.reference_entry <= 0 or self.stop_price <= 0:
            raise ValueError("entry and stop must be positive")
        if self.side == "BUY" and self.stop_price >= self.reference_entry:
            raise ValueError("BUY stop must be below entry")
        if self.side == "SELL" and self.stop_price <= self.reference_entry:
            raise ValueError("SELL stop must be above entry")


@dataclass(frozen=True)
class TradeState:
    trade_id: str
    phase: TradePhase = TradePhase.PLANNED
    state_revision: int = 0
    quantity: int = 0
    average_price: Decimal | None = None
    completed_target_ids: frozenset[str] = field(default_factory=frozenset)
    add_count: int = 0
    trailing_extreme: Decimal | None = None
    confirmed_stop: Decimal | None = None
    pending_stop: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise ValueError("trade_id is required")
        if self.state_revision < 0:
            raise ValueError("state_revision cannot be negative")
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        if self.quantity == 0 and self.average_price is not None:
            raise ValueError("average_price requires a non-zero quantity")
        if self.quantity > 0 and self.average_price is None:
            raise ValueError("non-zero quantity requires average_price")
