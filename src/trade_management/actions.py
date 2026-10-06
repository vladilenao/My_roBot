from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum


class EntryOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


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
    order_type: EntryOrderType = EntryOrderType.MARKET
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_entry_order(self)


@dataclass(frozen=True)
class AddToTrade(TradeAction):
    quantity: int
    order_type: EntryOrderType = EntryOrderType.MARKET
    limit_price: Decimal | None = None
    requested_quantity: int | None = None
    limiting_constraint: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_entry_order(self)
        if self.requested_quantity is not None and (isinstance(self.requested_quantity, bool)
                or not isinstance(self.requested_quantity, int) or self.requested_quantity < 0):
            raise ValueError("requested_quantity must be a non-negative integer")


@dataclass(frozen=True)
class ReduceTrade(TradeAction):
    quantity: int
    target_id: str | None = None
    reference_price: Decimal | None = None


@dataclass(frozen=True)
class CloseTrade(TradeAction):
    reference_price: Decimal | None = None


@dataclass(frozen=True)
class MoveStop(TradeAction):
    stop_price: Decimal


@dataclass(frozen=True)
class CancelEntry(TradeAction):
    pass


def _validate_entry_order(action: OpenTrade | AddToTrade) -> None:
    kind = EntryOrderType(action.order_type)
    object.__setattr__(action, "order_type", kind)
    if kind is EntryOrderType.LIMIT:
        if isinstance(action.quantity, bool) or not isinstance(action.quantity, int) or action.quantity <= 0:
            raise ValueError("limit entry requires positive integer quantity")
        if not isinstance(action.limit_price, Decimal) or not action.limit_price.is_finite() or action.limit_price <= 0:
            raise ValueError("limit entry requires a finite positive limit_price")
    elif action.limit_price is not None:
        raise ValueError("market entry cannot carry a limit_price")


def action_payload(action: TradeAction) -> dict[str, object]:
    data = asdict(action)
    data["type"] = type(action).__name__
    if isinstance(action, (OpenTrade, AddToTrade)) and action.order_type is EntryOrderType.MARKET:
        data.pop("order_type")
        data.pop("limit_price")
    if data.get("reference_price") is None:
        data.pop("reference_price", None)
    for key in ("requested_quantity", "limiting_constraint"):
        if data.get(key) is None:
            data.pop(key, None)
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in data.items()}


def action_from_payload(payload: dict[str, object]) -> TradeAction:
    data = dict(payload)
    name = data.pop("type")
    for key in ("stop_price", "limit_price", "reference_price"):
        if data.get(key) is not None:
            data[key] = Decimal(str(data[key]))
    constructors = {
        "OpenTrade": OpenTrade, "AddToTrade": AddToTrade, "ReduceTrade": ReduceTrade,
        "CloseTrade": CloseTrade, "MoveStop": MoveStop, "CancelEntry": CancelEntry,
    }
    if name not in constructors:
        raise ValueError(f"unknown action type {name!r}")
    return constructors[name](**data)
