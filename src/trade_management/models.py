from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Iterator, Mapping

from src.trade_management.actions import EntryOrderType, TradeAction

R_PRECISION = Decimal("0.01")
LEGACY_ALGORITHM = "legacy-v1"
CURRENT_ALGORITHM = "economics-v2"


def _nonnegative_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")


@dataclass(frozen=True)
class CostSnapshot:
    """Оценка на контракт: комиссия за сторону и запас проскальзывания за круг."""

    commission: Decimal = Decimal("1.5")
    slippage: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        _nonnegative_decimal(self.commission, "commission")
        _nonnegative_decimal(self.slippage, "slippage")

    @property
    def round_trip(self) -> Decimal:
        return self.commission * 2 + self.slippage

    @property
    def future_exit(self) -> Decimal:
        return self.commission + self.slippage / 2

    @classmethod
    def from_market(cls, market: Mapping[str, object]) -> "CostSnapshot":
        return cls(Decimal(str(market.get("entry_cost", 0))), Decimal(str(market.get("slippage_cost", 0))))


@dataclass(frozen=True)
class PortfolioBudgetPolicy:
    """Текущий общий процент счёта, не отдельный лимит сохранённого trade."""

    portfolio_pct: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        _nonnegative_decimal(self.portfolio_pct, "portfolio_pct")
        if self.portfolio_pct > 100:
            raise ValueError("portfolio_pct cannot exceed 100")

    def budget(self, balance: Decimal, equity: Decimal) -> Decimal:
        if not balance.is_finite() or not equity.is_finite():
            raise ValueError("account capital must be finite")
        return max(Decimal(0), min(balance, equity)) * self.portfolio_pct / 100


class TargetPriceBasis(StrEnum):
    R = "r"
    ABSOLUTE = "absolute"


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
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class StopBasis(StrEnum):
    """Why the planned protective stop sits where it does.

    Recorded in the plan so a trader or a later audit can tell a structure stop
    from one widened to the volatility floor or shortened to the volatility cap.
    """

    STRUCTURAL = "structural"
    ATR_FLOOR = "atr-floor"
    ATR_CAP = "atr-cap"


@dataclass(frozen=True)
class PlanEconomics:
    """Money view of a plan at the quantity actually admitted for it.

    The plan itself knows prices but not the size it was admitted at, so the
    monetary figures are computed by the trade manager once sizing is known and
    kept with the plan: they stay readable after a restart, and a report cannot
    quote a risk figure that belongs to a different quantity.
    """

    quantity: int
    risk_amount: Decimal
    reward_amount: Decimal | None
    costs_amount: Decimal
    payoff_ratio: Decimal | None
    fixed_reward_amount: Decimal | None = None
    fixed_quantity: int = 0
    target_quantities: Mapping[str, int] = field(default_factory=dict)
    slippage_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("economics require a positive quantity")
        for name in ("risk_amount", "costs_amount", "slippage_amount", "reward_amount", "fixed_reward_amount"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_decimal(value, name)
        if self.payoff_ratio is not None and self.risk_amount <= 0:
            raise ValueError("payoff ratio requires a positive risk amount")
        if self.payoff_ratio is not None and (self.reward_amount is None or not self.payoff_ratio.is_finite()):
            raise ValueError("payoff ratio requires known reward and a finite value")
        if not 0 <= self.fixed_quantity <= self.quantity:
            raise ValueError("fixed_quantity exceeds admitted quantity")
        if any(isinstance(q, bool) or not isinstance(q, int) or q < 0 for q in self.target_quantities.values()):
            raise ValueError("target quantities must be non-negative integers")
        if sum(self.target_quantities.values()) > self.quantity:
            raise ValueError("target quantities exceed admitted quantity")
        object.__setattr__(self, "target_quantities", _freeze(self.target_quantities))

    @property
    def budget_risk_amount(self) -> Decimal:
        return self.risk_amount + self.costs_amount

    @property
    def net_reward_amount(self) -> Decimal | None:
        return None if self.reward_amount is None else self.reward_amount - self.costs_amount


@dataclass(frozen=True)
class TargetPlan:
    target_id: str
    price: Decimal
    share: Decimal
    initial_step: Decimal = Decimal("0")
    price_basis: TargetPriceBasis = TargetPriceBasis.R

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_basis", TargetPriceBasis(self.price_basis))
        if not self.target_id:
            raise ValueError("target_id is required")
        if self.price <= 0:
            raise ValueError("target price must be positive")
        if self.initial_step < 0:
            raise ValueError("target initial step must not be negative")
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
    timeframe: str = ""
    stop_basis: StopBasis = StopBasis.STRUCTURAL
    economics: PlanEconomics | None = None
    algorithm_version: str = LEGACY_ALGORITHM
    cost_snapshot: CostSnapshot | None = None
    admission_snapshot: Mapping[str, object] = field(default_factory=dict)
    entry_order_type: EntryOrderType = EntryOrderType.MARKET
    requested_quantity: int | None = None
    price_step: Decimal | None = None

    def __post_init__(self) -> None:
        if self.price_step is not None:
            _nonnegative_decimal(self.price_step, "price_step")
            if self.price_step == 0:
                raise ValueError("price_step must be positive")
        if self.requested_quantity is not None and (isinstance(self.requested_quantity, bool) or not isinstance(self.requested_quantity, int) or self.requested_quantity < 0):
            raise ValueError("requested_quantity must be a nonnegative integer")
        if self.algorithm_version not in {LEGACY_ALGORITHM, CURRENT_ALGORITHM}:
            raise ValueError("unsupported trade algorithm_version")
        object.__setattr__(self, "entry_order_type", EntryOrderType(self.entry_order_type))
        admission = dict(self.admission_snapshot)
        for key in ("portfolio_pct", "min_trade_risk_pct", "min_risk_cost_ratio", "min_net_payoff", "max_slippage_r", "go_per_contract"):
            if key in admission:
                admission[key] = Decimal(str(admission[key]))
                _nonnegative_decimal(admission[key], key)
        if "portfolio_pct" in admission:
            PortfolioBudgetPolicy(admission["portfolio_pct"])
        object.__setattr__(self, "admission_snapshot", _freeze(admission))
        if self.algorithm_version == CURRENT_ALGORITHM and (
            self.cost_snapshot is None or self.entry_order_type is not EntryOrderType.LIMIT
        ):
            raise ValueError("economics-v2 requires a cost snapshot and limit entry")
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
        object.__setattr__(self, "targets", self._with_initial_steps())

    def _with_initial_steps(self) -> tuple[TargetPlan, ...]:
        """Freeze each target's distance from the entry as the step it may keep.

        Targets built before this field existed carry no step, so it is read
        back from the prices the plan already records. Deriving it here rather
        than in each profile means a new profile cannot forget to record one.
        """
        direction = Decimal("1") if self.side == "BUY" else Decimal("-1")
        return tuple(
            replace(target, initial_step=direction * (target.price - self.reference_entry))
            if target.initial_step == 0
            else target
            for target in self.targets
        )

    @property
    def risk_per_unit(self) -> Decimal:
        """Distance from the reference entry to the protective stop."""
        return abs(self.reference_entry - self.stop_price)

    @property
    def expected_r(self) -> Decimal | None:
        """Expected result in risk units: target shares divided by the risk.

        It is a property of the plan itself, so it is computed here rather than
        by whatever layer happens to display the plan.  ``None`` means the plan
        claims no fixed expectancy at all — it has no targets, or its stop sits on
        the entry — which is not the same claim as ``0R``: a zero reads as "this
        trade earns nothing", while a profile that exits on market conditions
        simply does not state an expectancy.
        """
        if self.algorithm_version == CURRENT_ALGORITHM:
            if self.economics is None or self.economics.reward_amount is None or self.economics.risk_amount <= 0:
                return None
            return self.economics.reward_amount / self.economics.risk_amount
        if not self.targets or self.risk_per_unit == 0:
            return None
        total = Decimal("0")
        for target in self.targets:
            move = target.price - self.reference_entry
            if self.side == "SELL":
                move = -move
            total += target.share * (move / self.risk_per_unit)
        return total.quantize(R_PRECISION, rounding=ROUND_HALF_UP)


def rebase_on_average(plan: TradePlan, average_price: Decimal) -> TradePlan:
    """Rebase a plan onto the price the entry actually filled at.

    The planned distances are kept rather than the planned prices: the entry
    gap consumes part of the stop distance before the trade even exists, so a
    plan kept at the signal price would quote targets and a stop that no longer
    correspond to the risk actually taken. Both the simulator and the journal
    call this on the same inputs, which is what keeps them in agreement. It is
    idempotent: re-running it on an already rebased plan reproduces the same
    prices, because the step is fixed at plan time and the prices are derived
    from it rather than from each other.
    """
    direction = Decimal("1") if plan.side == "BUY" else Decimal("-1")
    def grid(price, *, target):
        if plan.algorithm_version == LEGACY_ALGORITHM or plan.price_step is None:
            return price
        from decimal import ROUND_CEILING, ROUND_FLOOR
        upward = (plan.side == "BUY") == target
        return (price/plan.price_step).to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR)*plan.price_step
    targets = tuple(
        replace(target, price=grid(average_price + direction * target.initial_step, target=True))
        if plan.algorithm_version == LEGACY_ALGORITHM or target.price_basis is TargetPriceBasis.R
        else target
        for target in plan.targets
    )
    return replace(
        plan,
        reference_entry=average_price,
        stop_price=grid(average_price - direction * plan.risk_per_unit, target=False),
        targets=targets,
    )


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
    initial_stop_distance: Decimal | None = None
    max_quantity: int = 0
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    fees_known: bool = False
    trailing_active: bool = False
    adds_disabled: bool = False
    target_prices: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_prices", _freeze(self.target_prices))
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


@dataclass(frozen=True)
class RejectionReason:
    """Машиночитаемый код + человекочитаемое описание причины недопуска сигнала."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code is required")
        if not self.message:
            raise ValueError("message is required")


@dataclass(frozen=True)
class SignalAdmission:
    """Результат допуска сигнала: допущенные действия и причины недопуска.

    Итерируется как кортеж допущенных действий для совместимости с прежним
    возвращаемым типом ``tuple[TradeAction, ...]``.
    """

    actions: tuple[TradeAction, ...] = ()
    rejections: tuple[RejectionReason, ...] = ()
    plan: TradePlan | None = None
    diagnostics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", _freeze(self.diagnostics))

    def __iter__(self) -> Iterator[TradeAction]:
        return iter(self.actions)

    def __len__(self) -> int:
        return len(self.actions)

    def __getitem__(self, index):
        return self.actions[index]


_REJECTION_MESSAGES = {
    "no-contract-metadata": "Нет метаданных контракта для инструмента",
    "contract-expiring": "Контракт скоро истекает, вход запрещён",
    "unknown-profile": "Неизвестный профиль управления",
    "zero-quantity": "Размер позиции ниже минимального",
    "duplicate-signal": "дублирующий сигнал, сделка не взята в работу",
    "admission-error": "Ошибка при допуске сигнала",
    "risk-or-margin-budget": "не хватает лимитов риска или гарантийного обеспечения",
    "risk-budget": "не хватает лимита риска для входа",
    "risk-state-unknown": "неизвестен текущий риск открытого портфеля",
    "risk-cost-ratio": "риск до стопа слишком мал относительно издержек",
    "slippage-risk-ratio": "запас проскальзывания превышает допустимую долю риска",
    "payoff-below-floor": "чистая плановая прибыль относительно риска ниже порога",
    "stop-bounds-conflict": "минимальное расстояние стопа превышает допустимый потолок",
    "margin-committed-by-pending-orders": "маржа занята незаполненными заявками",
    "margin-budget": "не хватает бюджета гарантийного обеспечения",
    "cost-exceeds-reward": "расходы по сделке превышают плановую доходность",
    "risk-below-floor": "доступный объём не набирает минимальный риск сделки",
    "insufficient-history": "Недостаточно истории для расчёта",
    "missing-structure": "Нет подтверждённой структуры",
    "missing-pattern-context": "Нет подтверждённых ориентиров формации",
    "target-not-ahead": "Цель не впереди входа",
    "gap-entry": "вход по цене, ушедшей от сигнальной",
    "direction-not-allowed": "направление не разрешено для этого типа инструмента",
}


def rejection_message(code: str) -> str:
    """Человекочитаемое описание причины недопуска; fallback — сырой код."""
    return _REJECTION_MESSAGES.get(code, code)


def rejection_reason(code: str, *, message: str | None = None) -> RejectionReason:
    """Собрать RejectionReason, подставляя известный текст для кода."""
    return RejectionReason(code=code, message=message or rejection_message(code))
