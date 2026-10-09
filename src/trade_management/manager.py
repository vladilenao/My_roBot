"""Workflow coordinator for durable, addressed trade management."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus, FeeAdjustment, resolve_execution_fee
from src.logging_setup import get_logger
from src.portfolio.risk import PortfolioRiskManager, RiskLimits, RiskTrade
from src.scheduler.clock import Clock, as_aware, as_clock
from src.scheduler.timing import tf_period_minutes
from src.strategies.contracts import SignalType
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.observations import observe_bar
from src.trade_journal.storage import RecoveredTrade, ReservationCandidate, Storage
from src.trade_management.actions import (
    AddToTrade, CancelEntry, CloseTrade, MoveStop, OpenTrade, ReduceTrade, TradeAction,
    action_from_payload, action_payload,
)
from src.trade_management.errors import (
    InvalidStopBoundaryError,
    ReservationRejected,
    TradeManagementException,
    RiskStateUnknown,
)
from src.trade_management.audit import (
    CalculationTraceRepository,
    MeasuredValue,
    TraceLinks,
    TraceOutcome,
    calculation_trace,
)
from src.trade_management.economics import plan_economics
from src.trade_management.models import (
    PlanEconomics, PortfolioBudgetPolicy, SignalAdmission, TradePhase, TradePlan, rebase_on_average, rejection_message, rejection_reason,
)
from src.trade_management.opposite_signals import handle_raw_opposite_signal
from src.trade_management.pipeline import (
    PROFILE_CLASSES,
    build_management_market,
    build_profile_snapshot,
)
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult
from src.trade_management.notification import lifecycle_snapshot
from src.events.visual import market_snapshot, utc
from src.scheduler.timing import CandleScheduler


log = get_logger(__name__)


_INCREASES = (OpenTrade, AddToTrade)
_MANAGEABLE = {TradePhase.OPEN, TradePhase.BUILDING, TradePhase.REDUCING}
_TERMINAL = {TradePhase.CLOSED, TradePhase.CANCELLED}
_DEFAULT_RISK_LIMITS = RiskLimits(
    per_trade=Decimal("100"),
    per_instrument=Decimal("100"),
    per_group={},
    portfolio=Decimal("2"),
)
STOP_BOUNDARY_REASON = "stop_loss_beyond_market_boundary"
GAP_ENTRY_REASON = "gap-entry"
DEFAULT_SLIPPAGE_TOLERANCE = Decimal("0.005")
BUDGET_REJECTION_CODES = frozenset(
    {"risk-budget", "margin-committed-by-pending-orders", "margin-budget"}
)


@dataclass(frozen=True)
class _Sizing:
    """Результат расчёта размера входа и причина отказа бюджета, если он есть."""

    quantity: int
    risk_amount: Decimal
    code: str | None
    candidate_quantity: int
    per_unit_risk: Decimal
    go: Decimal
    used_risk: Decimal
    used_margin: Decimal
    risk_budget: Decimal
    margin_budget: Decimal
    limiting_constraint: str | None = None


@dataclass(frozen=True)
class _BudgetState:
    base: Decimal
    limit: Decimal
    open_risk: Decimal
    pending_risk: Decimal
    open_margin: Decimal
    pending_margin: Decimal
    trades: tuple[RiskTrade, ...]
    future_cost_sources: Mapping[str, str]

    @property
    def used_risk(self):
        return self.open_risk + self.pending_risk

    @property
    def used_margin(self):
        return self.open_margin + self.pending_margin


def _is_opposite(side: str, signal_type: SignalType) -> bool:
    return (side == SignalType.BUY.value and signal_type is SignalType.SELL) or (
        side == SignalType.SELL.value and signal_type is SignalType.BUY
    )


def _expires_within(meta, now: datetime, block_days: int) -> bool:
    """Близость экспирации контракта к моменту ``now`` (оба в naive UTC).

    ``expiration_date`` у акций отсутствует (``None``) — такие контракты всегда
    считаются неистекшими. Порог трактуется как календарные сутки: частичные
    сутки попадают в него.
    """
    expiration_date = getattr(meta, "expiration_date", None)
    if expiration_date is None:
        return False
    return (expiration_date - now).total_seconds() <= block_days * 86400


def plan_reference_entry(plan_json: str) -> Decimal:
    """Planned entry price of a stored plan, as recorded in its immutable json."""
    return Decimal(str(json.loads(plan_json)["reference_entry"]))


def _entry_ttl_seconds(timeframe: str) -> int:
    """Срок ожидания исполнения входа в секундах по таймфрейму сделки.

    Пустой таймфрейм (планы до внедрения этого правила) и крупные таймфреймы
    получают потолок 24 часа, чтобы незаполненный вход не жил вечно.
    """
    if not timeframe:
        return 24 * 3600
    minutes = tf_period_minutes(timeframe)
    if minutes <= 30:
        return 3600
    if minutes < 240:
        return 4 * 3600
    return 24 * 3600


class TradeManager:
    """Coordinates workflow intent without duplicating factual broker state.

    SQLite remains authoritative for plans, orders, and confirmed fills.  This
    class keeps no position cache; ``restore`` always reads the durable snapshot.
    """

    def __init__(
        self,
        storage: Storage,
        broker: BrokerPort,
        *,
        initial_balance: Decimal = Decimal("0"),
        post_fill_check: Callable[[ExecutionEvent], tuple[TradeAction, ...]] | None = None,
        budget_observer: Callable[[Mapping[str, object]], None] | None = None,
        execution_observer: Callable[[ExecutionEvent, Mapping[str, object]], None] | None = None,
        profiles_config: Mapping[str, Mapping[str, object]] | None = None,
        risk_limits: RiskLimits | None = None,
        max_qty: int | None = None,
        commission: Decimal | float | str | None = None,
        slippage: Decimal | float | str | None = None,
        slippage_tolerance: Decimal | float | str | None = None,
        signal_filter: object | None = None,
        contract_expiry_block_days: int = 2,
        direction_limits: Mapping[str, Sequence[str]] | None = None,
        min_trade_risk_pct: Decimal | float | str | None = None,
        portfolio_pct: Decimal | float | str | None = None,
        min_risk_cost_ratio: Decimal | float | str = Decimal("2"),
        min_net_payoff: Decimal | float | str = Decimal("1.5"),
        max_slippage_r: Decimal | float | str = Decimal("0.25"),
        clock: Clock | Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._broker = broker
        self._clock = as_clock(clock)
        self._reducer = ExecutionReducer(storage)
        self._initial_balance = initial_balance
        self._post_fill_check = post_fill_check
        self._budget_observer = budget_observer
        self._execution_observer = execution_observer
        self._visual_instruments = {}
        self._visual_markets = {}
        self._last_budget_alert = None
        self._profiles_config = dict(profiles_config or {})
        self._risk_limits = risk_limits or _DEFAULT_RISK_LIMITS
        self._max_qty = max_qty if isinstance(max_qty, int) and max_qty > 0 else 10
        self._commission = Decimal(str(commission)) if commission is not None else Decimal("0")
        self._slippage = Decimal(str(slippage)) if slippage is not None else Decimal("0")
        self._slippage_tolerance = (
            Decimal(str(slippage_tolerance)) if slippage_tolerance is not None else DEFAULT_SLIPPAGE_TOLERANCE
        )
        self._signal_filter = signal_filter
        self._risk = PortfolioRiskManager()
        self._traces = CalculationTraceRepository(storage)
        self._contract_expiry_block_days = contract_expiry_block_days
        self._direction_limits = {
            instrument_type: frozenset(str(direction).lower() for direction in directions)
            for instrument_type, directions in (direction_limits or {}).items()
        }
        self._min_trade_risk_pct = (
            Decimal(str(min_trade_risk_pct)) if min_trade_risk_pct is not None else Decimal("0")
        )
        self._budget_policy = PortfolioBudgetPolicy(Decimal(str(
            portfolio_pct if portfolio_pct is not None else risk_limits.portfolio if risk_limits is not None else 2
        )))
        self._min_risk_cost_ratio = Decimal(str(min_risk_cost_ratio))
        self._min_net_payoff = Decimal(str(min_net_payoff))
        self._max_slippage_r = Decimal(str(max_slippage_r))
        self._equity_unknown = False

    def restore(self) -> tuple[RecoveredTrade, ...]:
        """Return pending and open trades solely from the SQLite snapshot."""
        recovered = self._storage.load_trades()
        register = getattr(self._broker, "register_trade", None)
        if callable(register):
            for trade in recovered:
                register(trade.plan)
        resume = getattr(self._broker, "resume_trade", None)
        if callable(resume):
            for trade in recovered:
                resume(trade)
        return recovered

    def submit_plan(
        self,
        plan: TradePlan,
        action: OpenTrade,
        *,
        reservation: ReservationCandidate | None = None,
        risk_budget: Decimal | None = None,
        margin_budget: Decimal | None = None,
        traces: tuple[object, ...] = (),
        price_step: Decimal | None = None,
        step_cost: Decimal | None = None,
    ) -> bool:
        """Persist a newly planned entry and its durable broker intent atomically."""
        if action.trade_id != plan.trade_id or action.state_revision != 0:
            raise ValueError("opening action must own the new plan at revision 0")
        if action.quantity <= 0:
            raise ValueError("opening quantity must be positive")
        if reservation is not None and reservation.order_id != action.command_id:
            raise ValueError("reservation order_id must equal the opening command_id")
        if (risk_budget is None) != (margin_budget is None):
            raise ValueError("risk and margin budgets must be provided together")
        now = plan.created_at.isoformat()
        with self._storage.transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM processed_signals WHERE assignment_id = ? AND signal_id = ?",
                (plan.assignment_id, plan.signal_id),
            ).fetchone()
            if duplicate:
                return False
            existing = connection.execute(
                "SELECT 1 FROM trades WHERE trade_id = ?", (plan.trade_id,)
            ).fetchone()
            if existing:
                return False
            connection.execute(
                "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, "
                "plan_json, profile_json, phase, state_revision, profile_state_json, "
                "created_at, updated_at, price_step, step_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'ENTRY_PENDING', 0, '{}', ?, ?, ?, ?)",
                (plan.trade_id, plan.assignment_id, plan.instrument_id, plan.signal_id, plan.side,
                 json.dumps(_plan_payload(plan), sort_keys=True),
                 json.dumps(_profile_payload(plan), sort_keys=True), now, now,
                 _factor_text(price_step), _factor_text(step_cost)),
            )
            connection.execute(
                "INSERT INTO positions VALUES (?, ?, 0, NULL, '0', '0', '0', ?)",
                (plan.trade_id, plan.side, now),
            )
            connection.execute(
                "INSERT INTO protection VALUES (?, NULL, NULL, NULL, NULL, ?)", (plan.trade_id, now),
            )
            for index, target in enumerate(plan.targets):
                connection.execute(
                    "INSERT INTO targets (target_id, trade_id, target_index, price, "
                    "planned_quantity, filled_quantity, status) VALUES (?, ?, ?, ?, 0, 0, 'PENDING')",
                    (target.target_id, plan.trade_id, index, str(target.price)),
                )
            connection.execute(
                "INSERT INTO processed_signals VALUES (?, ?, ?, ?)",
                (plan.assignment_id, plan.signal_id, plan.trade_id, now),
            )
            self._insert_action(connection, action, now)
            self._ensure_account(connection, now)
            self._reserve(connection, reservation, risk_budget, margin_budget, now)
            for trace in traces:
                self._traces.record_in_transaction(connection, trace)
        register = getattr(self._broker, "register_trade", None)
        if callable(register):
            register(plan)
        return True

    def _assert_stop_within_market(
        self, action: TradeAction, market_close: Decimal | float | None
    ) -> None:
        """Reject a protective stop that already lies beyond the market.

        A stop past the market for a long (above ``close``) or a short (below
        ``close``) fills at once at a worse price than the stop itself, so it
        converts the intended protection into a realised loss. The rejection is
        recorded on its own transaction and only then raised: a trace written
        inside the rolled-back step transaction would not survive the rollback.

        Without a market price (empty frame, no candles for the instrument) the
        check is skipped fail-safe and logged, so a missing feed can never
        freeze emergency protection.
        """
        if not isinstance(action, MoveStop):
            return
        if market_close is None:
            log.warning(
                "submit_action %s: нет цены рынка, граница стопа не проверена",
                action.trade_id,
            )
            return
        row = self._storage.connection.execute(
            "SELECT side FROM trades WHERE trade_id = ?", (action.trade_id,)
        ).fetchone()
        if row is None:
            return
        side, = row
        close = Decimal(str(market_close))
        stop = action.stop_price
        beyond_market = stop >= close if side == "BUY" else stop <= close
        if not beyond_market:
            return
        self._traces.record(
            calculation_trace(
                "management.stop-boundary",
                inputs={
                    "side": MeasuredValue(side, "side"),
                    "stop_price": MeasuredValue(stop, "price"),
                    "market_close": MeasuredValue(close, "price"),
                },
                result=MeasuredValue(False, "rejected"),
                reason=STOP_BOUNDARY_REASON,
                formula="long: stop < market_close; short: stop > market_close",
                links=TraceLinks(trade_id=action.trade_id),
                outcome=TraceOutcome.REJECTED,
            )
        )
        raise InvalidStopBoundaryError(
            f"stop {stop} beyond market {close} for {action.trade_id}"
        )

    def submit_action(
        self,
        action: TradeAction,
        *,
        assignment_id: str | None = None,
        trace=None,
        market_close: Decimal | float | None = None,
    ) -> bool:
        """Validate and durably queue one management action before broker delivery.

        ``market_close`` is the last known market price of the instrument. For a
        protective stop it must lie on the position side of the market; a stop
        already beyond the market would fill immediately at a worse price and
        amplify the loss it is meant to contain. The check runs before the
        step transaction so the rejection trace commits on its own and nothing
        is left half-written.
        """
        self._assert_stop_within_market(action, market_close)
        if isinstance(action, (CloseTrade, ReduceTrade)) and action.reference_price is None and market_close is not None:
            action = replace(action, reference_price=Decimal(str(market_close)))
        now = as_aware(self._clock.now()).isoformat()
        with self._storage.transaction() as connection:
            row = connection.execute(
                "SELECT assignment_id, phase, state_revision FROM trades WHERE trade_id = ?", (action.trade_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown trade")
            owner, phase, revision = row
            if assignment_id is not None and assignment_id != owner:
                raise ValueError("action does not belong to assignment")
            if action.state_revision != revision:
                raise ValueError("stale-state-revision")
            self._validate_phase(action, TradePhase(phase))
            if connection.execute("SELECT 1 FROM outbox WHERE command_id = ?", (action.command_id,)).fetchone():
                return False
            reservation = None
            if isinstance(action, AddToTrade):
                if action.order_type == "market":
                    if market_close is None:
                        raise ValueError("new addition requires its own limit price")
                    action = replace(action, order_type="limit", limit_price=Decimal(str(market_close)))
                action, reservation = self._size_addition(connection, action)
            self._insert_action(connection, action, now)
            if reservation is not None:
                budget = self._budget_state(connection)
                self._reserve(connection, reservation, budget.limit, budget.base, now)
            if isinstance(action, MoveStop):
                connection.execute(
                    "UPDATE protection SET pending_stop=?, pending_command_id=?, updated_at=? WHERE trade_id=?",
                    (str(action.stop_price), action.command_id, now, action.trade_id),
                )
            if trace is not None:
                self._traces.record_in_transaction(connection, trace)
        return True

    def _entry_gap(self, event: ExecutionEvent) -> tuple[str, Decimal, Decimal, int] | None:
        """Adverse entry deviation in price units, or ``None`` when not applicable.

        Reads the confirmed average price of the position rather than the fill
        price of the event: an averaged add-to-trade must be judged as a whole,
        because a single cheap leg does not cancel an expensive one.
        """
        if event.trade_id is None or event.status is not ExecutionStatus.FILL:
            return None
        cursor = self._storage.connection.execute(
            "SELECT o.action_type, t.side, t.plan_json, t.state_revision, p.average_price "
            ", o.requested_price FROM orders o JOIN trades t ON t.trade_id = o.trade_id "
            "JOIN positions p ON p.trade_id = t.trade_id WHERE o.command_id = ?",
            (event.command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        record = dict(zip((column[0] for column in cursor.description), row, strict=True))
        if str(record["action_type"]).upper().split(":", 1)[0] not in {"OPEN", "ADD"}:
            return None
        if record["average_price"] is None:
            return None
        reference = Decimal(str(plan_reference_entry(record["plan_json"])))
        average = Decimal(str(record["average_price"]))
        side = str(record["side"]).upper()
        payload = self._storage.connection.execute(
            "SELECT payload_json FROM outbox WHERE command_id=?", (event.command_id,),
        ).fetchone()
        if payload and json.loads(payload[0]).get("order_type") == "limit":
            reference = Decimal(str(record["requested_price"]))
            average = event.price
        adverse = average - reference if side == "BUY" else reference - average
        if adverse <= 0:
            return None
        return side, reference, average, int(record["state_revision"])

    def _rebase_open_targets(self, event: ExecutionEvent) -> None:
        """Restate unfilled targets and the stop on the confirmed average price.

        The planned prices stay in ``plan_json``: they are what the trade was
        admitted on and what the journal replays. Only live facts move, and
        only for targets that have not traded yet, so a filled target keeps the
        price it actually executed at.
        """
        if event.trade_id is None:
            return
        cursor = self._storage.connection.execute(
            "SELECT o.action_type, t.plan_json, t.trade_id, t.side, p.average_price "
            "FROM orders o JOIN trades t ON t.trade_id = o.trade_id "
            "JOIN positions p ON p.trade_id = t.trade_id WHERE o.command_id = ?",
            (event.command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return
        record = dict(zip((column[0] for column in cursor.description), row, strict=True))
        if str(record["action_type"]).upper().split(":", 1)[0] not in {"OPEN", "ADD"}:
            return
        if record["average_price"] is None:
            return
        recovered = self._storage.load_trade(event.trade_id, include_terminal=True)
        if recovered is None:
            return
        if event.status is ExecutionStatus.PARTIAL and recovered.plan.algorithm_version == "legacy-v1":
            return
        rebased = rebase_on_average(recovered.plan, Decimal(str(record["average_price"])))
        now = as_aware(self._clock.now()).isoformat()
        with self._storage.transaction() as connection:
            for target in rebased.targets:
                connection.execute(
                    "UPDATE targets SET price = ? WHERE trade_id = ? AND target_id = ? "
                    "AND filled_quantity = 0",
                    (str(target.price), event.trade_id, target.target_id),
                )
            connection.execute(
                "UPDATE protection SET confirmed_stop = ?, updated_at = ? WHERE trade_id = ?",
                (str(
                    max(rebased.stop_price, recovered.state.confirmed_stop) if rebased.side == "BUY"
                    else min(rebased.stop_price, recovered.state.confirmed_stop)
                ) if recovered.plan.algorithm_version == "economics-v2" and recovered.state.confirmed_stop is not None
                 else str(rebased.stop_price), now, event.trade_id),
            )
        log.info(
            "цели и стоп %s пересчитаны от средней %s", event.trade_id, record["average_price"],
        )

    def _enforce_entry_slippage_guard(self, event: ExecutionEvent) -> None:
        """Reject an entry filled beyond tolerance from the price of its signal.

        A gap against the position hurts twice: the stop distance planned from
        the signal is already spent by the fill, so the trade starts with more
        risk than was admitted and sized for. The deviation is measured on the
        confirmed average price, the rejection is recorded durably, and the
        position is queued to be closed at market instead of being held.
        """
        measured = self._entry_gap(event)
        if measured is None:
            return
        side, reference, average, revision = measured
        allowed = reference * self._slippage_tolerance
        deviation = (average - reference) if side == "BUY" else (reference - average)
        if deviation <= allowed:
            return
        log.warning(
            "вход %s вне допуска: цена %s против сигнальной %s (%s)",
            event.trade_id, average, reference, GAP_ENTRY_REASON,
        )
        self._traces.record(
            calculation_trace(
                "execution.slippage_guard",
                inputs={
                    "signal_price": MeasuredValue(reference, "price"),
                    "filled_price": MeasuredValue(average, "price"),
                    "side": MeasuredValue(side, "side"),
                    "tolerance": MeasuredValue(self._slippage_tolerance, "fraction"),
                },
                result=MeasuredValue(deviation, "price"),
                outcome=TraceOutcome.REJECTED,
                reason=GAP_ENTRY_REASON,
                formula="|filled - signal| / signal, неблагоприятное направление",
                links=TraceLinks(trade_id=event.trade_id),
            )
        )
        self.submit_action(
            CloseTrade(
                command_id=f"{event.trade_id}:{GAP_ENTRY_REASON}:{event.command_id}",
                trade_id=event.trade_id,
                state_revision=revision,
                reason=GAP_ENTRY_REASON,
            )
        )
        now = as_aware(self._clock.now()).isoformat()
        with self._storage.transaction() as connection:
            connection.execute(
                "UPDATE trades SET phase = 'REJECTED', state_revision = state_revision + 1, updated_at = ? "
                "WHERE trade_id = ? AND phase NOT IN ('CLOSED', 'CANCELLED', 'REJECTED', 'ERROR')",
                (now, event.trade_id),
            )

    def dispatch(self, now: datetime, *, limit: int = 100) -> tuple[ExecutionEvent, ...]:
        """Deliver persisted outbox commands and immediately consume their outcomes."""
        events: list[ExecutionEvent] = []
        for command in self._storage.claim_outbox(limit):
            action = _action_from_payload(command.payload)
            event = self._broker.submit(action, now)
            self._storage.mark_outbox_sent(command.command_id, sent_at=now)
            self.consume(event)
            events.append(event)
        return tuple(events)

    def consume(self, event: ExecutionEvent | FeeAdjustment) -> bool:
        """Reduce one broker event, then schedule post-fill risk/protection checks."""
        if isinstance(event, FeeAdjustment):
            return self._reducer.apply_fee_adjustment(event)
        previous_protection = self._storage.connection.execute(
            "SELECT s.confirmed_stop,p.quantity FROM protection s JOIN positions p USING(trade_id) WHERE s.trade_id=?",
            (event.trade_id,)).fetchone()
        if event.fee is None:
            row = self._storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id=?", (event.trade_id,)).fetchone()
            snapshot = json.loads(row[0]).get("cost_snapshot") if row else None
            event = resolve_execution_fee(event, None if snapshot is None else Decimal(str(snapshot["commission"])))
        applied = self._reducer.apply(event)
        if not applied:
            return False
        stop_confirmed = event.status is ExecutionStatus.ACK and self._confirm_stop(event)
        if event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}:
            self._enforce_entry_slippage_guard(event)
            self._rebase_open_targets(event)
            actual_stop = self._storage.connection.execute("SELECT confirmed_stop FROM protection WHERE trade_id=?",
                                                         (event.trade_id,)).fetchone()
            if actual_stop and actual_stop[0] is not None and previous_protection and (
                    previous_protection[1] == 0 or previous_protection[0] != actual_stop[0]):
                with self._storage.transaction() as connection:
                    payload = json.loads(connection.execute("SELECT payload_json FROM events WHERE event_id=?",
                                                            (event.execution_id,)).fetchone()[0])
                    payload["confirmed_stop"] = {"old": previous_protection[0] if previous_protection[1] else None,
                                                 "new": actual_stop[0], "effective_at": event.timestamp.isoformat()}
                    connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                                       (json.dumps(payload, sort_keys=True), event.execution_id))
            self._record_portfolio_budget(event_id=event.execution_id)
        if event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL} and self._post_fill_check:
            for action in self._post_fill_check(event):
                self.submit_action(action)
        if stop_confirmed or event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL, ExecutionStatus.CANCEL, ExecutionStatus.REJECT}:
            self._notify_execution(event)
        return True

    def configure_visual_context(self, instruments):
        """Runtime metadata, independent of the notification channel."""
        self._visual_instruments.update({i.ticker: i for i in instruments})

    def remember_market(self, instrument, timeframe, frame, as_of):
        self._visual_instruments[instrument.ticker] = instrument
        self._visual_markets[instrument.ticker, timeframe] = market_snapshot(frame, timeframe, as_of)

    def _notify_execution(self, event):
        if self._execution_observer is None:
            return
        row = self._storage.connection.execute(
            "SELECT o.action_type,t.instrument_id,t.side,p.quantity,p.realized_pnl,p.fees,t.price_step,t.step_cost,o.quantity,b.payload_json "
            "FROM orders o JOIN trades t ON t.trade_id=o.trade_id JOIN positions p ON p.trade_id=t.trade_id "
            "JOIN outbox b ON b.command_id=o.command_id "
            "WHERE o.command_id=?", (event.command_id,)).fetchone()
        if row is None:
            return
        action, instrument, side, quantity, gross, fees, step, cost, selected, payload = row
        details = {"action_type": action, "instrument_id": instrument, "side": side, "quantity_remaining": quantity,
                   "gross_pnl": Decimal(gross), "fees_total": Decimal(fees), "net_pnl": Decimal(gross)-Decimal(fees),
                   "pnl_units": "RUB" if step is not None and cost is not None else "RAW",
                   "fees_known": self._storage.connection.execute("SELECT 1 FROM fills WHERE trade_id=? AND fee_source='unknown' LIMIT 1", (event.trade_id,)).fetchone() is None}
        if action == "ADD":
            metadata = json.loads(payload)
            details.update(selected_quantity=selected, requested_quantity=metadata.get("requested_quantity"),
                            limiting_constraint=metadata.get("limiting_constraint"))
        try:
            # A separate read transaction cannot write or change trade state.
            connection = self._storage.connection
            connection.execute("BEGIN")
            try:
                plan_row = connection.execute("SELECT plan_json FROM trades WHERE trade_id=?", (event.trade_id,)).fetchone()
                timeframe = json.loads(plan_row[0]).get("timeframe", "")
                market = self._visual_markets.get((instrument, timeframe))
                if market is not None:
                    grid = CandleScheduler(timeframe)
                    market = dict(market, candles=tuple(c for c in market["candles"]
                                                       if utc(grid.bar_close(utc(c["time"]))) <= utc(event.timestamp)))
                contract_for = getattr(self._broker, "contract_for", None)
                meta = contract_for(instrument) if callable(contract_for) else None
                details["visual"] = lifecycle_snapshot(connection, event,
                    instrument=self._visual_instruments.get(instrument), meta=meta, market=market)
            finally:
                connection.rollback()
            self._execution_observer(event, details)
        except Exception as exc:
            log.warning("Не удалось доставить подтверждённое исполнение: %s", exc)

    def actions_for_signal(
        self,
        assignment,
        decision,
        instrument,
        frame,
        context=None,
        *,
        timeframe: str | None = None,
    ) -> SignalAdmission:
        """Admit one raw strategy event: manage an owned trade or plan a new entry.

        Returns admitted actions plus the reasons why a tradable signal was not
        admitted. HOLD yields an empty admission without reasons (no signal).
        """
        if decision.signal_type is SignalType.HOLD:
            return SignalAdmission()
        timeframe = timeframe or assignment.timeframe
        contract = getattr(self._broker, "contract_for", None)
        meta = contract(instrument.ticker) if callable(contract) else None
        if meta is None:
            log.debug("actions_for_signal %s: нет метаданных контракта", instrument.ticker)
            return SignalAdmission(
                rejections=(rejection_reason("no-contract-metadata"),),
            )
        if _expires_within(meta, self._clock.now(), self._contract_expiry_block_days):
            log.info("actions_for_signal %s: вход отклонён — близкая экспирация", instrument.ticker)
            return SignalAdmission(
                rejections=(rejection_reason("contract-expiring"),),
            )
        try:
            recovered = self.restore()
            owned = self._owned_trade(recovered, assignment.id)
            if owned is not None:
                actions = self._manage_owned(owned, assignment, decision, instrument, frame, context, meta, timeframe)
                return SignalAdmission(actions=actions)
            return self._plan_entry(assignment, decision, instrument, frame, context, meta, timeframe)
        except sqlite3.IntegrityError as exc:
            log.warning("actions_for_signal %s: %s", instrument.ticker, exc, exc_info=True)
            return SignalAdmission(
                rejections=(rejection_reason(
                    "admission-error",
                    message="Ошибка при допуске сигнала: Ошибка на уровне БД: нарушение целостности данных",
                ),),
            )
        except ReservationRejected as rejected:
            log.info("actions_for_signal %s: резервирование отклонено — %s", instrument.ticker, rejected.code)
            return SignalAdmission(
                rejections=(rejection_reason(rejected.code),),
            )
        except RiskStateUnknown as exc:
            log.warning("неизвестен риск портфеля: %s", exc)
            self._traces.record(calculation_trace(
                "portfolio.admission", inputs={"unknown_reason": MeasuredValue(str(exc), "reason")},
                result=MeasuredValue(False, "allowed"), reason="risk-state-unknown",
                formula="all open exposure and account factors must be known",
                links=TraceLinks(assignment_id=assignment.id, signal_id=decision.event_id),
                outcome=TraceOutcome.REJECTED, algorithm_version="economics-v2",
            ))
            return SignalAdmission(rejections=(rejection_reason("risk-state-unknown"),))
        except Exception as exc:
            log.warning("actions_for_signal %s: %s", instrument.ticker, exc, exc_info=True)
            return SignalAdmission(
                rejections=(rejection_reason(
                    "admission-error",
                    message=f"Ошибка при допуске сигнала: {rejection_message(str(exc))}",
                ),),
            )

    def manage(
        self,
        instrument,
        assignments,
        frame,
        context=None,
        *,
        timeframe: str | None = None,
        now: datetime | None = None,
    ) -> tuple[TradeAction, ...]:
        """Protect each live trade of the instrument from the provided frame."""
        now = now or as_aware(self._clock.now())
        contract = getattr(self._broker, "contract_for", None)
        meta = contract(instrument.ticker) if callable(contract) else None
        if meta is None:
            return ()
        if _expires_within(meta, now.replace(tzinfo=None), self._contract_expiry_block_days):
            return self._close_expiring(instrument.ticker)
        results: list[TradeAction] = []
        results.extend(self._cancel_stale_entries(instrument.ticker, now))
        for recovered in self.restore():
            plan, state = recovered.plan, recovered.state
            if plan.instrument_id != instrument.ticker or state.phase not in _MANAGEABLE or state.quantity <= 0:
                continue
            profile_cls = PROFILE_CLASSES.get(plan.profile.name)
            if profile_cls is None:
                continue
            market = build_management_market(
                contract=meta,
                frame=frame,
                context=context,
                profile_parameters=dict(plan.profile.parameters),
                signal=False,
                commission=self._commission,
                slippage=self._slippage,
            )
            self._enrich_owned_market(recovered, market)
            try:
                profile = profile_cls()
                result, management_trace = profile.manage_with_trace(ManagementContext(plan, state, market))
            except Exception as exc:
                log.warning("manage %s: %s", plan.trade_id, exc)
                continue
            self._save_profile_result(recovered, result, management_trace)
            for action in result.actions:
                if isinstance(action, AddToTrade):
                    continue
                try:
                    if self.submit_action(
                        action,
                        assignment_id=plan.assignment_id,
                        trace=management_trace,
                        market_close=market.get("close"),
                    ):
                        results.append(action)
                except (ValueError, TradeManagementException) as exc:
                    log.debug("manage %s пропустил %s: %s", plan.trade_id, type(action).__name__, exc)
        return tuple(results)

    def _enrich_owned_market(self, recovered, market):
        """Supply durable factors and causality to versioned profile management."""
        step, cost = self._storage.connection.execute("SELECT price_step,step_cost FROM trades WHERE trade_id=?",
                                                     (recovered.plan.trade_id,)).fetchone()
        if recovered.plan.algorithm_version == "economics-v2":
            market["price_step"] = None if step is None else Decimal(step)
            market["step_cost"] = None if cost is None else Decimal(cost)
        else:
            economics = recovered.plan.economics
            market["legacy_costs_known"] = economics is not None
            # Legacy BE keeps its old per-contract input and formula, but a
            # changed config must not supply an invented historical policy.
            market["entry_cost"] = economics.costs_amount/economics.quantity if economics is not None else None
            market["exit_cost"] = Decimal(0)
            market["slippage_cost"] = Decimal(0)
        market["pending_increase"] = self._storage.connection.execute(
            "SELECT 1 FROM orders WHERE trade_id=? AND action_type IN ('OPEN','ADD') AND status IN ('PENDING','ACK','PARTIAL')",
            (recovered.plan.trade_id,)).fetchone() is not None
        fills = self._storage.connection.execute(
            "SELECT f.price,f.executed_at FROM fills f JOIN orders o ON o.order_id=f.order_id "
            "WHERE f.trade_id=? AND o.action_type IN ('OPEN','ADD') ORDER BY f.executed_at,f.rowid",
            (recovered.plan.trade_id,)).fetchall()
        if fills:
            market["last_entry_price"] = Decimal(fills[-1][0])
            bar_time = market.get("created_at")
            first_time = as_aware(datetime.fromisoformat(fills[0][1]))
            market["holding_bar_eligible"] = bool(bar_time and (
                as_aware(bar_time) > first_time or
                as_aware(bar_time) == first_time and market.get("open") == Decimal(fills[0][0])))

    def _save_profile_result(self, recovered, result, trace):
        with self._storage.transaction() as connection:
            if trace is not None:
                self._traces.record_in_transaction(connection, trace)
            if result.state:
                current = connection.execute("SELECT profile_state_json FROM trades WHERE trade_id=?", (recovered.plan.trade_id,)).fetchone()
                values = {**json.loads(current[0]), **result.state}
                connection.execute("UPDATE trades SET profile_state_json=? WHERE trade_id=?",
                                   (json.dumps(_json_value(values), sort_keys=True), recovered.plan.trade_id))

    def _close_expiring(self, ticker: str) -> tuple[TradeAction, ...]:
        """Принудительная защита сделок по контракту с близкой экспирацией.

        Незаполненные входы отменяются, живые позиции закрываются. Профильные
        расчёты для этого инструмента пропускаются. Повторные тики после
        подтверждения брокера отфильтровываются переходами фаз (``CANCELLED``,
        остаток ``0``).
        """
        submitted: list[TradeAction] = []
        for recovered in self.restore():
            plan, state = recovered.plan, recovered.state
            if plan.instrument_id != ticker or state.phase is not TradePhase.ENTRY_PENDING:
                continue
            log.info("manage %s: отмена незаполненного входа %s (близкая экспирация)", ticker, plan.trade_id)
            action = CancelEntry(
                command_id=uuid4().hex,
                trade_id=plan.trade_id,
                state_revision=state.state_revision,
                reason="contract-expiring",
            )
            if self.submit_action(action, assignment_id=plan.assignment_id):
                submitted.append(action)
        for recovered in self.restore():
            plan, state = recovered.plan, recovered.state
            if plan.instrument_id != ticker or state.phase not in _MANAGEABLE or state.quantity <= 0:
                continue
            log.info("manage %s: принудительное закрытие %s (близкая экспирация)", ticker, plan.trade_id)
            action = CloseTrade(
                command_id=uuid4().hex,
                trade_id=plan.trade_id,
                state_revision=state.state_revision,
                reason="contract-expiring",
            )
            if self.submit_action(action, assignment_id=plan.assignment_id):
                submitted.append(action)
        return tuple(submitted)

    def _cancel_stale_entries(self, ticker: str, now: datetime) -> tuple[TradeAction, ...]:
        """Отменить незаполненный вход, чья принятая заявка не исполнилась в срок.

        Отсчёт ведётся от момента подтверждения (ACK) заявки входа, который
        сохраняется в ``updated_at`` OPEN-ордера. Сделки без подтверждённой
        заявки пропускаются: заявка ещё не размещена или время недоступно.
        """
        submitted: list[TradeAction] = []
        for recovered in self.restore():
            plan, state = recovered.plan, recovered.state
            if plan.instrument_id != ticker or state.phase is not TradePhase.ENTRY_PENDING:
                continue
            ack = recovered.entry_ack_at
            if ack is None:
                continue
            if now - ack <= timedelta(seconds=_entry_ttl_seconds(plan.timeframe)):
                continue
            log.info("manage %s: отмена незаполненного входа %s (истёк срок ожидания исполнения)", ticker, plan.trade_id)
            action = CancelEntry(
                command_id=uuid4().hex,
                trade_id=plan.trade_id,
                state_revision=state.state_revision,
                reason="entry-timeout",
            )
            if self.submit_action(action, assignment_id=plan.assignment_id):
                submitted.append(action)
        return tuple(submitted)

    def _manage_owned(self, owned, assignment, decision, instrument, frame, context, meta, timeframe) -> tuple[TradeAction, ...]:
        plan, state = owned.plan, owned.state
        profile_cls = PROFILE_CLASSES.get(plan.profile.name)
        if profile_cls is None:
            return ()
        market = build_management_market(
            contract=meta,
            frame=frame,
            context=context,
            profile_parameters=dict(plan.profile.parameters),
            price=decision.price,
            signal=True,
            commission=self._commission,
            slippage=self._slippage,
        )
        self._enrich_owned_market(owned, market)
        if _is_opposite(plan.side, decision.signal_type):
            result = handle_raw_opposite_signal(plan, state, decision)
            actions = result.actions
            management_trace = calculation_trace(
                "management.opposite-signal",
                inputs={
                    "current_side": MeasuredValue(plan.side, "side"),
                    "signal": MeasuredValue(decision.signal_type.value, "signal"),
                    "quantity": MeasuredValue(state.quantity, "contracts"),
                },
                result=MeasuredValue([type(action).__name__ for action in actions], "actions"),
                reason="opposite-signal-management",
                formula="opposite signal + current position -> management actions",
                links=TraceLinks(assignment_id=assignment.id, trade_id=plan.trade_id,
                                 signal_id=decision.event_id),
            )
        else:
            if self._signal_filter is not None:
                apply_with_trace = getattr(self._signal_filter, "apply_with_trace", None)
                if callable(apply_with_trace):
                    filtered, filter_trace = apply_with_trace(
                        decision, context, profile_name=assignment.filter_profile,
                        instrument=instrument.ticker, timeframe=timeframe
                    )
                    if filter_trace is not None:
                        self._traces.record(filter_trace)
                else:
                    filtered = self._signal_filter.apply(
                        decision,
                        context,
                        profile_name=assignment.filter_profile,
                        instrument=instrument,
                        timeframe=timeframe,
                    )
                if filtered.signal_type is SignalType.HOLD:
                    return ()
            profile = profile_cls()
            management_result, management_trace = profile.manage_with_trace(ManagementContext(plan, state, market))
            self._save_profile_result(owned, management_result, management_trace)
            actions = tuple(
                action for action in management_result.actions
                if isinstance(action, AddToTrade)
            )
        submitted: list[TradeAction] = []
        for action in actions:
            if isinstance(action, AddToTrade):
                action = replace(action, order_type="limit", limit_price=Decimal(str(decision.price)))
            try:
                if self.submit_action(
                    action,
                    assignment_id=assignment.id,
                    trace=management_trace,
                    market_close=market.get("close"),
                ):
                    payload = self._storage.connection.execute("SELECT payload_json FROM outbox WHERE command_id=?", (action.command_id,)).fetchone()
                    submitted.append(_action_from_payload(json.loads(payload[0])))
            except (ValueError, KeyError, TypeError) as exc:
                log.debug("actions_for_signal %s пропустил %s: %s", plan.trade_id, type(action).__name__, exc)
        return tuple(submitted)

    def _plan_entry(self, assignment, decision, instrument, frame, context, meta, timeframe) -> SignalAdmission:
        instrument_type = getattr(instrument, "instrument_type", None)
        allowed = self._direction_limits.get(instrument_type) if instrument_type is not None else None
        if allowed is not None:
            direction = "short" if decision.signal_type is SignalType.SELL else "long"
            if direction not in allowed:
                log.info(
                    "actions_for_signal %s: вход %s не разрешён для типа %r",
                    instrument.ticker, direction, instrument_type,
                )
                return SignalAdmission(
                    rejections=(rejection_reason("direction-not-allowed"),),
                )
        profile_cls = PROFILE_CLASSES.get(assignment.management)
        if profile_cls is None:
            log.warning("Неизвестный профиль управления %r", assignment.management)
            return SignalAdmission(
                rejections=(rejection_reason("unknown-profile", message=f"Неизвестный профиль управления {assignment.management!r}"),),
            )
        snapshot = build_profile_snapshot(
            assignment.management, self._profiles_config.get(assignment.management, {})
        )
        market = build_management_market(
            contract=meta,
            frame=frame,
            context=context,
            profile_parameters=dict(snapshot.parameters),
            price=decision.price,
            signal=False,
            commission=self._commission,
            slippage=self._slippage,
            admission_snapshot={
                "portfolio_pct": self._budget_policy.portfolio_pct,
                "go_per_contract": Decimal(str(meta.go_sell if decision.signal_type is SignalType.SELL else meta.go_buy)),
                "min_trade_risk_pct": self._min_trade_risk_pct,
                "min_risk_cost_ratio": self._min_risk_cost_ratio,
                "min_net_payoff": self._min_net_payoff,
                "max_slippage_r": self._max_slippage_r,
            },
        )
        trade_id = decision.event_id or f"{assignment.id}:{timeframe}:{decision.signal_type.value}"
        profile = profile_cls()
        plan, plan_trace = profile.plan_with_trace(
            PlanningContext(
                trade_id=trade_id,
                assignment_id=assignment.id,
                instrument_id=instrument.ticker,
                signal=decision,
                profile=snapshot,
                market=market,
            )
        )
        if isinstance(plan, ProfileResult) or plan is None:
            code = str((plan.state or {}).get("reason", "profile-rejected")) if isinstance(plan, ProfileResult) else "profile-rejected"
            self._traces.record(replace(
                plan_trace,
                links=replace(plan_trace.links, trade_id=None),
            ))
            return SignalAdmission(
                rejections=(rejection_reason(code),),
            )
        plan = replace(plan, timeframe=timeframe or "")
        if self._storage.connection.execute(
            "SELECT 1 FROM processed_signals WHERE assignment_id=? AND signal_id=?",
            (plan.assignment_id, plan.signal_id),
        ).fetchone():
            return SignalAdmission(rejections=(rejection_reason("duplicate-signal"),))
        sizing = self._size_open_quantity(plan, meta)
        quantity, risk_amount = sizing.quantity, sizing.risk_amount
        budget = self._budget_base()
        if quantity <= 0:
            if sizing.code in BUDGET_REJECTION_CODES:
                self._log_budget_rejection(
                    sizing.code,
                    used_risk=sizing.used_risk,
                    candidate_risk=sizing.per_unit_risk * sizing.candidate_quantity,
                    risk_budget=sizing.risk_budget,
                    used_margin=sizing.used_margin,
                    candidate_margin=sizing.go * sizing.candidate_quantity,
                    margin_budget=sizing.margin_budget,
                )
            self._traces.record(calculation_trace(
                "portfolio.position_sizing",
                inputs={
                    "risk_budget": MeasuredValue(
                        sizing.risk_budget, "RUB"
                    ),
                    "candidate_quantity": MeasuredValue(quantity, "contracts"),
                    "risk_amount": MeasuredValue(risk_amount, "RUB"),
                },
                result=MeasuredValue(quantity, "contracts"),
                outcome=TraceOutcome.REJECTED,
                reason=sizing.code or "risk-limit-rejects-entry",
                formula="no quantity satisfies risk, exposure, and margin limits",
                links=TraceLinks(assignment_id=assignment.id, signal_id=plan.signal_id),
            ))
            return SignalAdmission(
                rejections=(rejection_reason(sizing.code or "zero-quantity"),),
            )
        economics = plan_economics(
            plan,
            quantity=quantity,
            price_step=Decimal(str(meta.price_step)),
            step_cost=Decimal(str(meta.step_cost)),
            market=market,
        )
        plan = replace(plan, economics=economics)
        refused = self._entry_economics_refusal(plan, economics, quantity, risk_amount, budget)
        if refused is not None:
            return refused
        action = OpenTrade(
            command_id=f"{plan.trade_id}:entry",
            trade_id=plan.trade_id,
            state_revision=0,
            reason="profile-entry",
            quantity=quantity,
            order_type=plan.entry_order_type,
            limit_price=plan.reference_entry if plan.entry_order_type == "limit" else None,
        )
        go = Decimal(str(meta.go_buy if plan.side == "BUY" else meta.go_sell))
        reservation = ReservationCandidate(
            reservation_id=f"reservation:{plan.trade_id}",
            trade_id=plan.trade_id,
            order_id=action.command_id,
            priority=assignment.priority,
            assignment_id=assignment.id,
            instrument_id=plan.instrument_id,
            signal_id=plan.signal_id,
            risk_amount=risk_amount,
            margin_amount=go * quantity,
        )
        sizing_trace = calculation_trace(
            "portfolio.position_sizing",
            inputs={
                "risk_budget": MeasuredValue(sizing.risk_budget, "RUB"),
                "budget_base": MeasuredValue(budget, "RUB"),
                "portfolio_pct": MeasuredValue(self._budget_policy.portfolio_pct, "%"),
                "used_risk": MeasuredValue(sizing.used_risk, "RUB"),
                "free_risk": MeasuredValue(max(Decimal(0), sizing.risk_budget-sizing.used_risk), "RUB"),
                "used_margin": MeasuredValue(sizing.used_margin, "RUB"),
                "requested_quantity": MeasuredValue(plan.requested_quantity, "contracts"),
                "selected_quantity": MeasuredValue(quantity, "contracts"),
                "limiting_constraint": MeasuredValue(sizing.limiting_constraint, "constraint"),
                "candidate_quantity": MeasuredValue(quantity, "contracts"),
                "risk_amount": MeasuredValue(risk_amount, "RUB"),
            },
            result=MeasuredValue(quantity, "contracts"),
            reason="risk-and-exposure-within-limits",
            formula="largest quantity satisfying risk, exposure, and margin limits",
            links=TraceLinks(assignment_id=assignment.id, trade_id=plan.trade_id, signal_id=plan.signal_id),
        )
        accepted = self.submit_plan(
            plan,
            action,
            reservation=reservation,
            risk_budget=self._budget_policy.budget(budget, budget),
            margin_budget=budget,
            traces=(plan_trace, sizing_trace),
            price_step=Decimal(str(meta.price_step)),
            step_cost=Decimal(str(meta.step_cost)),
        )
        if accepted:
            return SignalAdmission(actions=(action,), plan=plan, diagnostics={
                **self.portfolio_diagnostics(), "requested_quantity": plan.requested_quantity,
                "selected_quantity": quantity, "limiting_constraint": sizing.limiting_constraint})
        return SignalAdmission(
            rejections=(rejection_reason("duplicate-signal"),),
        )

    def _entry_economics_refusal(
        self,
        plan: TradePlan,
        economics: PlanEconomics,
        quantity: int,
        risk_amount: Decimal,
        budget: Decimal,
    ) -> SignalAdmission | None:
        """Отказать вход, когда разрешённый объём не имеет смысла попробовать.

        Обе проверки стоят после риск-размера и до резервирования: расходы и
        минимальный риск известны только при фактическом объёме, а резервировать
        заведомо убыточную или неосмысленно мелкую позицию незачем.  Проверка
        расходов не выполняется для плана без целей — у него нет планового
        дохода, с которым расходы можно сравнить.
        """
        risk_budget = self._budget_policy.budget(budget, budget)
        min_risk = budget * self._min_trade_risk_pct / Decimal("100")
        inputs = {
            "quantity": quantity, "risk_amount": economics.risk_amount,
            "costs_amount": economics.costs_amount, "slippage_amount": economics.slippage_amount,
            "reward_amount": economics.reward_amount, "payoff_ratio": economics.payoff_ratio,
            "min_risk": min_risk, "min_risk_cost_ratio": self._min_risk_cost_ratio,
            "max_slippage_r": self._max_slippage_r, "min_net_payoff": self._min_net_payoff,
        }
        def refuse(code):
            self._traces.record(calculation_trace(
                "portfolio.entry-economics",
                inputs={key: MeasuredValue(value, "contracts" if key == "quantity" else
                        "ratio" if key in {"payoff_ratio", "min_risk_cost_ratio", "max_slippage_r", "min_net_payoff"} else "RUB")
                        for key, value in inputs.items()},
                result=MeasuredValue(False, "allowed"), reason=code,
                formula="reward>C; R>=minimum; R/C>=threshold; S/R<=threshold; net payoff>=threshold",
                links=TraceLinks(assignment_id=plan.assignment_id, signal_id=plan.signal_id),
                outcome=TraceOutcome.REJECTED, algorithm_version=plan.algorithm_version,
            ))
            return SignalAdmission(rejections=(rejection_reason(code),), diagnostics={
                **self.portfolio_diagnostics(), "algorithm_version": plan.algorithm_version,
                "risk_amount": economics.risk_amount, "costs_amount": economics.costs_amount,
                "slippage_amount": economics.slippage_amount, "payoff_ratio": economics.payoff_ratio,
                "selected_quantity": quantity,
                "threshold": {"risk-below-floor": min_risk, "risk-cost-ratio": self._min_risk_cost_ratio,
                              "slippage-risk-ratio": self._max_slippage_r, "payoff-below-floor": self._min_net_payoff}.get(code),
            })

        if economics.reward_amount is not None and economics.reward_amount > 0 and (
            economics.costs_amount >= economics.reward_amount
        ):
            self._log_economics_rejection(
                "cost-exceeds-reward",
                quantity=quantity,
                risk_amount=risk_amount,
                reward_amount=economics.reward_amount,
                costs_amount=economics.costs_amount,
            )
            return refuse("cost-exceeds-reward")
        if self._min_trade_risk_pct > 0 and economics.risk_amount < min_risk:
            self._log_economics_rejection(
                "risk-below-floor",
                quantity=quantity,
                risk_amount=risk_amount,
                min_risk=min_risk,
                risk_budget=risk_budget,
            )
            return refuse("risk-below-floor")
        reason = None
        if self._min_risk_cost_ratio > 0 and economics.costs_amount > 0 and economics.risk_amount / economics.costs_amount < self._min_risk_cost_ratio:
            reason = "risk-cost-ratio"
        elif self._max_slippage_r > 0 and economics.risk_amount > 0 and economics.slippage_amount / economics.risk_amount > self._max_slippage_r:
            reason = "slippage-risk-ratio"
        elif self._min_net_payoff > 0 and economics.payoff_ratio is not None and economics.payoff_ratio < self._min_net_payoff:
            reason = "payoff-below-floor"
        if reason:
            self._log_economics_rejection(reason, quantity=quantity, risk_amount=economics.risk_amount,
                                         costs_amount=economics.costs_amount, slippage_amount=economics.slippage_amount,
                                         threshold=inputs[{"risk-cost-ratio": "min_risk_cost_ratio", "slippage-risk-ratio": "max_slippage_r", "payoff-below-floor": "min_net_payoff"}[reason]])
            return refuse(reason)
        return None

    def _log_economics_rejection(self, code: str, **numbers: Decimal | int) -> None:
        log.warning(
            "Вход отклонён по экономике плана: %s (%s)",
            code,
            ", ".join(f"{name}={value}" for name, value in numbers.items()),
        )

    def _size_open_quantity(self, plan: TradePlan, meta) -> _Sizing:
        """Size a fresh entry within risk and margin available after committed reserves.

        Свободный бюджет считается за вычетом сумм ACTIVE-резервов, поэтому
        расчётный объём по построению помещается в резервирование; если
        свободного бюджета не хватает даже на минимальный вход, размер равен
        нулю, а код причины называет исчерпанный бюджет.
        """
        value_per_point = Decimal(str(meta.step_cost)) / Decimal(str(meta.price_step))
        direction = Decimal("1") if plan.side == "BUY" else Decimal("-1")
        price_risk = direction * (plan.reference_entry - plan.stop_price) * value_per_point
        costs = plan.cost_snapshot.round_trip if plan.cost_snapshot else self._commission * 2 + self._slippage
        per_unit_risk = price_risk + costs
        state = self._budget_state(self._storage.connection)
        budget = state.base
        go = Decimal(str(meta.go_buy if plan.side == "BUY" else meta.go_sell))
        used_risk, used_margin = state.used_risk, state.used_margin
        risk_budget = state.limit
        free_risk = risk_budget - used_risk
        free_margin = budget - used_margin
        if self._budget_policy.portfolio_pct == 0 or used_risk > risk_budget:
            return _Sizing(0, Decimal(0), "risk-budget", 1, per_unit_risk, go, used_risk, used_margin, risk_budget, budget)
        if per_unit_risk <= 0 or price_risk <= 0:
            return _Sizing(0, Decimal("0"), "zero-quantity", 0, per_unit_risk, go,
                           used_risk, used_margin, risk_budget, budget)
        def risk_allowed(quantity: int) -> bool:
            return per_unit_risk * quantity <= free_risk

        def margin_allowed(quantity: int) -> bool:
            return go <= 0 or go * quantity <= free_margin

        def allowed(quantity: int) -> bool:
            return risk_allowed(quantity) and margin_allowed(quantity)

        maximum = min(self._max_qty, plan.requested_quantity) if plan.requested_quantity is not None else self._max_qty
        quantity = self._largest_allowed(allowed, maximum)
        if quantity > 0:
            constraint = ("risk" if not risk_allowed(quantity+1) else
                          "margin" if not margin_allowed(quantity+1) else
                          "requested-quantity" if plan.requested_quantity is not None and plan.requested_quantity <= self._max_qty else
                          "max-quantity")
            return _Sizing(quantity, per_unit_risk * quantity, None, quantity, per_unit_risk,
                           go, used_risk, used_margin, risk_budget, budget, constraint)
        risk_quantity = self._largest_allowed(risk_allowed, maximum)
        if risk_quantity > 0:
            code = "margin-committed-by-pending-orders" if state.pending_margin > 0 else "margin-budget"
        elif per_unit_risk > free_risk:
            code = "risk-budget"
        else:
            code = "zero-quantity"
        candidate_quantity = risk_quantity or 1
        return _Sizing(0, Decimal("0"), code, candidate_quantity, per_unit_risk, go,
                       used_risk, used_margin, risk_budget, budget)

    def _largest_allowed(self, allowed: Callable[[int], bool], maximum: int | None = None) -> int:
        low, high = 0, self._max_qty if maximum is None else maximum
        while low < high:
            candidate_qty = (low + high + 1) // 2
            if allowed(candidate_qty):
                low = candidate_qty
            else:
                high = candidate_qty - 1
        return low

    def _committed_budget(self) -> tuple[Decimal, Decimal]:
        return self._active_reservation_totals(self._storage.connection)

    @staticmethod
    def _active_reservation_totals(connection) -> tuple[Decimal, Decimal]:
        rows = connection.execute(
            "SELECT risk_amount, margin_amount FROM reservations WHERE status = 'ACTIVE'"
        ).fetchall()
        used_risk = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        used_margin = sum((Decimal(row[1]) for row in rows), Decimal("0"))
        return used_risk, used_margin

    @staticmethod
    def _log_budget_rejection(
        code: str | None,
        *,
        used_risk: Decimal,
        candidate_risk: Decimal,
        risk_budget: Decimal,
        used_margin: Decimal,
        candidate_margin: Decimal,
        margin_budget: Decimal,
    ) -> None:
        log.info(
            "отказ резервирования входа: code=%s used_risk=%s candidate_risk=%s "
            "risk_budget=%s used_margin=%s candidate_margin=%s margin_budget=%s",
            code, used_risk, candidate_risk, risk_budget,
            used_margin, candidate_margin, margin_budget,
        )

    def _budget_base(self) -> Decimal:
        row = self._storage.connection.execute(
            "SELECT balance, equity FROM account WHERE account_id = 1"
        ).fetchone()
        if row is None:
            return max(Decimal(0), self._initial_balance)
        return max(Decimal(0), min(Decimal(row[0]), Decimal(row[1])))

    def _budget_state(self, connection) -> _BudgetState:
        try:
            return self._known_budget_state(connection)
        except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise RiskStateUnknown(f"некорректные данные портфеля: {exc}") from exc

    def _known_budget_state(self, connection) -> _BudgetState:
        if self._equity_unknown:
            raise RiskStateUnknown("нет полной текущей переоценки счёта")
        capital = connection.execute("SELECT balance,equity FROM account WHERE account_id=1").fetchone()
        base = max(Decimal(0), min(Decimal(capital[0]), Decimal(capital[1]))) if capital else max(Decimal(0), self._initial_balance)
        pending_risk, pending_margin = self._active_reservation_totals(connection)
        if not base.is_finite() or not all(value.is_finite() and value >= 0 for value in (pending_risk, pending_margin)):
            raise RiskStateUnknown("некорректные суммы счёта/резервов")
        risk, margin, trades, sources = Decimal(0), Decimal(0), [], {}
        for trade_id, instrument, side, payload, step, cost, quantity, average, stop in connection.execute(
            "SELECT t.trade_id,t.instrument_id,t.side,t.plan_json,t.price_step,t.step_cost,p.quantity,p.average_price,s.confirmed_stop "
            "FROM trades t JOIN positions p ON p.trade_id=t.trade_id LEFT JOIN protection s ON s.trade_id=t.trade_id WHERE p.quantity>0",
        ):
            if step is None or cost is None or stop is None or average is None:
                raise RiskStateUnknown(f"неполные денежные факторы/защита сделки {trade_id}")
            plan = json.loads(payload)
            snapshot = plan.get("cost_snapshot")
            sources[trade_id] = "legacy-future-estimate" if snapshot is None else "saved-cost-snapshot"
            rate = self._commission if snapshot is None else Decimal(str(snapshot["commission"]))
            allowance = self._slippage if snapshot is None else Decimal(str(snapshot["slippage"]))
            try:
                item = RiskTrade(trade_id, instrument, frozenset(), side, quantity, Decimal(average), Decimal(stop),
                                 Decimal(step), Decimal(cost), expected_exit_cost=rate * quantity,
                                 slippage_allowance=allowance / 2 * quantity)
            except ValueError as exc:
                raise RiskStateUnknown(f"некорректные факторы сделки {trade_id}") from exc
            risk += self._risk.trade_risk(item).remaining_loss
            trades.append(item)
            getter = getattr(self._broker, "contract_for", None)
            meta = getter(instrument) if callable(getter) else None
            go = getattr(meta, "go_buy" if side == "BUY" else "go_sell", None) if meta is not None else None
            if go is None:
                go = plan.get("admission_snapshot", {}).get("go_per_contract")
            if go is None:
                raise RiskStateUnknown(f"неизвестно ГО сделки {trade_id}")
            go = Decimal(str(go))
            if not go.is_finite() or go < 0:
                raise RiskStateUnknown(f"некорректное ГО сделки {trade_id}")
            margin += go * quantity
        return _BudgetState(base, self._budget_policy.budget(base, base), risk, pending_risk, margin, pending_margin, tuple(trades), sources)

    def _record_portfolio_budget(self, *, event_id=None) -> None:
        """Audit current exposure; budget excess never creates a broker action."""
        try:
            state = self._budget_state(self._storage.connection)
        except RiskStateUnknown as exc:
            log.warning("Текущий риск неизвестен; новые увеличения запрещены: %s", exc)
            self._notify_budget(self.portfolio_diagnostics())
            return
        excess = max(Decimal(0), state.used_risk-state.limit)
        self._traces.record(calculation_trace(
            "portfolio.current-budget",
            inputs={
                "budget_base": MeasuredValue(state.base, "RUB"),
                "portfolio_pct": MeasuredValue(self._budget_policy.portfolio_pct, "%"),
                "risk_budget": MeasuredValue(state.limit, "RUB"),
                "open_risk": MeasuredValue(state.open_risk, "RUB"),
                "pending_risk": MeasuredValue(state.pending_risk, "RUB"),
                "free_risk": MeasuredValue(max(Decimal(0), state.limit-state.used_risk), "RUB"),
                "future_cost_sources": MeasuredValue(state.future_cost_sources, "provenance"),
            }, result=MeasuredValue(excess, "RUB"), reason="risk-budget" if excess else "within-portfolio-budget",
            formula="excess=max(0,open risk+unfilled reserves-B*portfolio_pct/100)",
            links=TraceLinks(event_id=event_id), algorithm_version="economics-v2",
            outcome=TraceOutcome.REJECTED if excess else TraceOutcome.ACCEPTED,
        ))
        if excess:
            log.warning("Общий риск %s ₽, лимит %s ₽, превышение %s ₽: новые входы/доборы запрещены",
                         state.used_risk, state.limit, excess)
            self._notify_budget(self._budget_details(state))
        else:
            self._last_budget_alert = None

    def _notify_budget(self, details):
        fingerprint = tuple(sorted(details.items()))
        if self._budget_observer is not None and fingerprint != self._last_budget_alert:
            try:
                self._budget_observer(details)
                self._last_budget_alert = fingerprint
            except Exception as exc:
                log.warning("Не удалось доставить диагностику портфельного бюджета: %s", exc)

    def _budget_details(self, state):
        return {"budget_base": state.base, "portfolio_pct": self._budget_policy.portfolio_pct,
                "risk_budget": state.limit, "open_risk": state.open_risk, "pending_risk": state.pending_risk,
                "free_risk": max(Decimal(0), state.limit-state.used_risk),
                "risk_excess": max(Decimal(0), state.used_risk-state.limit), "risk_state": "known"}

    def portfolio_diagnostics(self):
        """Self-contained budget view for admission and notification projections."""
        try:
            return self._budget_details(self._budget_state(self._storage.connection))
        except RiskStateUnknown as exc:
            result = {"portfolio_pct": self._budget_policy.portfolio_pct, "risk_state": "unknown",
                      "unknown_reason": str(exc).partition(" сделки ")[0]}
            try:
                base = self._budget_base()
                if base.is_finite():
                    result.update(budget_base=base, risk_budget=base*self._budget_policy.portfolio_pct/100)
            except (ValueError, ArithmeticError):
                pass
            return result

    def _size_addition(self, connection, action: AddToTrade):
        budget = self._budget_state(connection)
        current = next(t for t in budget.trades if t.trade_id == action.trade_id)
        row = connection.execute("SELECT assignment_id,signal_id,plan_json FROM trades WHERE trade_id=?", (action.trade_id,)).fetchone()
        data = json.loads(row[2])
        costs = data.get("cost_snapshot")
        rate = self._commission if costs is None else Decimal(str(costs["commission"]))
        slip = self._slippage if costs is None else Decimal(str(costs["slippage"]))
        getter = getattr(self._broker, "contract_for", None)
        meta = getter(current.instrument_id) if callable(getter) else None
        go = Decimal(str(getattr(meta, "go_buy" if current.side == "BUY" else "go_sell"))) if meta else Decimal(str(data["admission_snapshot"]["go_per_contract"]))
        before = self._risk.trade_risk(current).remaining_loss
        direction = Decimal(1) if current.side == "BUY" else Decimal(-1)
        def delta(q):
            # Weighted stop exposure before division avoids rounding a repeating
            # average and incorrectly refusing exact budget equality (500 ₽).
            exposure = ((current.average_price-current.stop_price)*current.quantity
                        + (action.limit_price-current.stop_price)*q)
            price = max(Decimal(0), direction * exposure / current.price_step * current.step_cost)
            return max(Decimal(0), price + current.expected_exit_cost + current.slippage_allowance + (2*rate+slip)*q - before)
        requested = action.requested_quantity if action.requested_quantity is not None else action.quantity
        maximum = min(action.quantity, requested, max(0, self._max_qty-current.quantity))
        if maximum <= 0:
            raise ValueError("zero-quantity")
        def risk_allowed(q):
            return self._budget_policy.portfolio_pct > 0 and budget.used_risk <= budget.limit and budget.used_risk+delta(q) <= budget.limit
        def allowed(q):
            return risk_allowed(q) and budget.used_margin+go*q <= budget.base
        quantity = self._largest_allowed(allowed, maximum)
        if quantity <= 0:
            code = "risk-budget" if self._largest_allowed(risk_allowed, maximum) == 0 else "margin-committed-by-pending-orders" if budget.pending_margin > 0 else "margin-budget"
            raise ReservationRejected(code, used_risk=budget.used_risk, candidate_risk=delta(1), risk_budget=budget.limit,
                                      used_margin=budget.used_margin, candidate_margin=go, margin_budget=budget.base)
        constraint = ("risk" if not risk_allowed(quantity+1) else "margin" if budget.used_margin+go*(quantity+1) > budget.base else
                      "max-quantity" if current.quantity+quantity >= self._max_qty else action.limiting_constraint or "profile")
        selected = replace(action, quantity=quantity, requested_quantity=requested, limiting_constraint=constraint)
        self._traces.record_in_transaction(connection, calculation_trace(
            "portfolio.addition-sizing",
            inputs={"budget_base": MeasuredValue(budget.base, "RUB"),
                    "risk_budget": MeasuredValue(budget.limit, "RUB"),
                    "open_risk": MeasuredValue(budget.open_risk, "RUB"),
                    "pending_risk": MeasuredValue(budget.pending_risk, "RUB"),
                    "used_margin": MeasuredValue(budget.used_margin, "RUB"),
                    "requested_quantity": MeasuredValue(requested, "contracts"),
                    "limiting_constraint": MeasuredValue(constraint, "constraint"),
                    "command_id": MeasuredValue(action.command_id, "command"),
                    "additional_risk": MeasuredValue(delta(quantity), "RUB"),
                    "limit_price": MeasuredValue(action.limit_price, "price")},
            result=MeasuredValue(quantity, "contracts"), reason="largest-permitted-integer-quantity",
            formula="resulting stop exposure + future costs + unfilled reserves <= account budget",
            links=TraceLinks(trade_id=action.trade_id),
            algorithm_version="economics-v2",
        ))
        candidate = ReservationCandidate(f"reservation:{action.command_id}", action.trade_id, action.command_id, 0,
                                         row[0], current.instrument_id, row[1], delta(quantity), go*quantity)
        return selected, candidate

    def mark_to_market(self, prices: Mapping[str, float | Decimal]) -> bool:
        """Полная переоценка после баровых исполнений, до допуска новых кандидатов."""
        floating = Decimal(0)
        for instrument, side, quantity, average, step, cost in self._storage.connection.execute(
            "SELECT t.instrument_id,t.side,p.quantity,p.average_price,t.price_step,t.step_cost "
            "FROM positions p JOIN trades t ON t.trade_id=p.trade_id WHERE p.quantity>0",
        ):
            if instrument not in prices or step is None or cost is None or average is None:
                self._equity_unknown = True
                log.warning("нет полной переоценки: неизвестны цена/факторы открытой позиции")
                return False
            price, step, cost = Decimal(str(prices[instrument])), Decimal(step), Decimal(cost)
            if not all(value.is_finite() and value > 0 for value in (price, step, cost)):
                self._equity_unknown = True
                return False
            direction = Decimal(1) if side == "BUY" else Decimal(-1)
            floating += direction*(price-Decimal(average))/step*cost*quantity
        with self._storage.transaction() as connection:
            row = connection.execute("SELECT balance FROM account WHERE account_id=1").fetchone()
            if row:
                connection.execute("UPDATE account SET equity=? WHERE account_id=1", (str(Decimal(row[0])+floating),))
        self._equity_unknown = False
        self._record_portfolio_budget()
        return True

    def observe_bars(self, prices, bar_times, *, timeframe="1m", gap_before=False):
        """Journal closed holding bars directly, independently of notifications."""
        with self._storage.transaction() as connection:
            return sum(observe_bar(connection, instrument, bar_times[instrument], bar,
                                   timeframe=timeframe, gap_before=gap_before)
                       for instrument, bar in prices.items() if instrument in bar_times)

    def _risk_trades(self) -> tuple[RiskTrade, ...]:
        return self._budget_state(self._storage.connection).trades

    @staticmethod
    def _owned_trade(recovered, assignment_id: str) -> RecoveredTrade | None:
        for trade in recovered:
            if trade.plan.assignment_id == assignment_id and trade.state.phase not in _TERMINAL:
                return trade
        return None

    @staticmethod
    def _validate_phase(action: TradeAction, phase: TradePhase) -> None:
        if isinstance(action, OpenTrade):
            allowed = phase is TradePhase.PLANNED
        elif isinstance(action, AddToTrade):
            allowed = phase in {TradePhase.OPEN, TradePhase.BUILDING}
        elif isinstance(action, CancelEntry):
            allowed = phase is TradePhase.ENTRY_PENDING
        else:
            allowed = phase in _MANAGEABLE
        if not allowed:
            raise ValueError(f"action {type(action).__name__} is invalid in phase {phase}")

    def _insert_action(self, connection, action: TradeAction, now: str) -> None:
        payload = _action_payload(action)
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, ?, 'PENDING', ?, NULL)",
            (action.command_id, action.trade_id, json.dumps(payload, sort_keys=True), now),
        )
        quantity = action.quantity if isinstance(action, (OpenTrade, AddToTrade, ReduceTrade)) else 1
        if isinstance(action, CloseTrade):
            row = connection.execute(
                "SELECT quantity FROM positions WHERE trade_id = ?", (action.trade_id,)
            ).fetchone()
            if row is None or row[0] <= 0:
                raise ValueError("cannot close a trade without an open quantity")
            quantity = row[0]
        requested_price = (
            str(action.stop_price) if isinstance(action, MoveStop)
            else str(action.limit_price) if isinstance(action, (OpenTrade, AddToTrade)) and action.limit_price is not None
            else None
        )
        action_type = {
            OpenTrade: "OPEN",
            AddToTrade: "ADD",
            ReduceTrade: "REDUCE",
            CloseTrade: "CLOSE",
            MoveStop: "MOVESTOP",
            CancelEntry: "CANCEL",
        }[type(action)]
        if isinstance(action, ReduceTrade) and action.target_id:
            action_type = f"TARGET:{action.target_id}"
        connection.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, 'PENDING', ?, 0, ?, ?, ?)",
            (action.command_id, action.trade_id, action.command_id, action_type, quantity,
             requested_price, now, now),
        )

    def _reserve(self, connection, candidate, risk_budget, margin_budget, now: str) -> None:
        if candidate is None:
            return
        if risk_budget is None or margin_budget is None:
            raise ValueError("reservation requires risk and margin budgets")
        state = self._budget_state(connection)
        used_risk, used_margin = state.used_risk, state.used_margin
        risk_budget = min(risk_budget, state.limit)
        margin_budget = min(margin_budget, state.base)
        if used_risk + candidate.risk_amount > risk_budget:
            code = "risk-budget"
        elif used_margin + candidate.margin_amount > margin_budget:
            code = "margin-committed-by-pending-orders" if state.pending_margin > 0 else "margin-budget"
        else:
            code = None
        if code is not None:
            self._log_budget_rejection(
                code,
                used_risk=used_risk,
                candidate_risk=candidate.risk_amount,
                risk_budget=risk_budget,
                used_margin=used_margin,
                candidate_margin=candidate.margin_amount,
                margin_budget=margin_budget,
            )
            raise ReservationRejected(
                code,
                used_risk=used_risk,
                candidate_risk=candidate.risk_amount,
                risk_budget=risk_budget,
                used_margin=used_margin,
                candidate_margin=candidate.margin_amount,
                margin_budget=margin_budget,
            )
        connection.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (candidate.reservation_id, candidate.trade_id, candidate.order_id,
             str(candidate.risk_amount), str(candidate.margin_amount), str(candidate.risk_amount),
             str(candidate.margin_amount), now, now),
        )

    def _ensure_account(self, connection, now: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO account VALUES (1, ?, ?, '0', '0', '0', ?)",
            (str(self._initial_balance), str(self._initial_balance), now),
        )

    def _confirm_stop(self, event: ExecutionEvent) -> bool:
        if event.reason == "next-bar":
            return False  # Request acceptance; simulator protection is not active yet.
        with self._storage.transaction() as connection:
            row = connection.execute(
                "SELECT requested_price FROM orders WHERE command_id = ? AND action_type = 'MOVESTOP'",
                (event.command_id,),
            ).fetchone()
            if row is not None:
                old = connection.execute("SELECT confirmed_stop FROM protection WHERE trade_id=?",
                                         (event.trade_id,)).fetchone()[0]
                connection.execute(
                    "UPDATE protection SET confirmed_stop=?, pending_stop=NULL, confirmed_order_id=?, "
                    "pending_command_id=NULL, updated_at=? WHERE trade_id=?",
                    (row[0], event.order_id, event.timestamp.isoformat(), event.trade_id),
                )
                connection.execute(
                    "UPDATE trades SET state_revision = state_revision + 1, updated_at = ? WHERE trade_id = ?",
                    (event.timestamp.isoformat(), event.trade_id),
                )
                payload = json.loads(connection.execute("SELECT payload_json FROM events WHERE event_id=?",
                                                        (event.execution_id,)).fetchone()[0])
                payload["confirmed_stop"] = {"old": old, "new": row[0], "effective_at": event.timestamp.isoformat()}
                connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                                   (json.dumps(payload, sort_keys=True), event.execution_id))
                return True
        return False


def _profile_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "name": plan.profile.name,
        "version": plan.profile.version,
        "parameters": _json_value(dict(plan.profile.parameters)),
    }


def _plan_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "reference_entry": str(plan.reference_entry), "stop_price": str(plan.stop_price),
        "targets": [
            {"target_id": target.target_id, "share": str(target.share), "price": str(target.price),
             "initial_step": str(target.initial_step), "price_basis": str(target.price_basis)}
            for target in plan.targets
        ],
        "timeframe": plan.timeframe,
        "stop_basis": str(plan.stop_basis),
        "economics": _economics_payload(plan.economics),
        "algorithm_version": plan.algorithm_version,
        "entry_order_type": str(plan.entry_order_type),
        "requested_quantity": plan.requested_quantity,
        "price_step": None if plan.price_step is None else str(plan.price_step),
        "cost_snapshot": None if plan.cost_snapshot is None else {
            "commission": str(plan.cost_snapshot.commission), "slippage": str(plan.cost_snapshot.slippage),
        },
        "admission_snapshot": _json_value(plan.admission_snapshot),
    }


def _economics_payload(economics: PlanEconomics | None) -> dict[str, object] | None:
    """Денежное представление плана в том же виде, в каком его читает журнал."""
    if economics is None:
        return None
    return {
        "quantity": economics.quantity,
        "risk_amount": str(economics.risk_amount),
        "reward_amount": None if economics.reward_amount is None else str(economics.reward_amount),
        "costs_amount": str(economics.costs_amount),
        "payoff_ratio": None if economics.payoff_ratio is None else str(economics.payoff_ratio),
        "fixed_reward_amount": None if economics.fixed_reward_amount is None else str(economics.fixed_reward_amount),
        "fixed_quantity": economics.fixed_quantity,
        "target_quantities": dict(economics.target_quantities),
        "slippage_amount": str(economics.slippage_amount),
    }


def _factor_text(value: Decimal | None) -> str | None:
    """Текстовое представление снапшота фактора контракта или ``None``."""
    return format(value, "f") if value is not None else None


def _action_payload(action: TradeAction) -> dict[str, object]:
    return action_payload(action)


def _action_from_payload(payload: dict[str, object]) -> TradeAction:
    return action_from_payload(payload)


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
