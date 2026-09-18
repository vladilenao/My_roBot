"""Workflow coordinator for durable, addressed trade management."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.logging_setup import get_logger
from src.portfolio.risk import PortfolioRiskManager, RiskLimits, RiskTrade
from src.strategies.contracts import SignalType
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import RecoveredTrade, ReservationCandidate, Storage
from src.trade_management.actions import (
    AddToTrade, CancelEntry, CloseTrade, MoveStop, OpenTrade, ReduceTrade, TradeAction,
)
from src.trade_management.audit import (
    CalculationTraceRepository,
    MeasuredValue,
    TraceLinks,
    calculation_trace,
)
from src.trade_management.models import SignalAdmission, TradePhase, TradePlan, rejection_reason
from src.trade_management.opposite_signals import handle_raw_opposite_signal
from src.trade_management.pipeline import (
    PROFILE_CLASSES,
    build_management_market,
    build_profile_snapshot,
)
from src.trade_management.profiles.base import ManagementContext, PlanningContext, ProfileResult


log = get_logger(__name__)


_INCREASES = (OpenTrade, AddToTrade)
_MANAGEABLE = {TradePhase.OPEN, TradePhase.BUILDING, TradePhase.REDUCING}
_TERMINAL = {TradePhase.CLOSED, TradePhase.CANCELLED}
_DEFAULT_RISK_LIMITS = RiskLimits(
    per_trade=Decimal("2"),
    per_instrument=Decimal("6"),
    per_group={},
    portfolio=Decimal("10"),
)


def _is_opposite(side: str, signal_type: SignalType) -> bool:
    return (side == SignalType.BUY.value and signal_type is SignalType.SELL) or (
        side == SignalType.SELL.value and signal_type is SignalType.BUY
    )


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
        profiles_config: Mapping[str, Mapping[str, object]] | None = None,
        risk_limits: RiskLimits | None = None,
        max_qty: int | None = None,
        commission: Decimal | float | str | None = None,
        slippage: Decimal | float | str | None = None,
        signal_filter: object | None = None,
    ) -> None:
        self._storage = storage
        self._broker = broker
        self._reducer = ExecutionReducer(storage)
        self._initial_balance = initial_balance
        self._post_fill_check = post_fill_check
        self._profiles_config = dict(profiles_config or {})
        self._risk_limits = risk_limits or _DEFAULT_RISK_LIMITS
        self._max_qty = max_qty if isinstance(max_qty, int) and max_qty > 0 else 10
        self._commission = Decimal(str(commission)) if commission is not None else Decimal("0")
        self._slippage = Decimal(str(slippage)) if slippage is not None else Decimal("0")
        self._signal_filter = signal_filter
        self._risk = PortfolioRiskManager()
        self._traces = CalculationTraceRepository(storage)

    def restore(self) -> tuple[RecoveredTrade, ...]:
        """Return pending and open trades solely from the SQLite snapshot."""
        recovered = self._storage.load_trades()
        register = getattr(self._broker, "register_trade", None)
        if callable(register):
            for trade in recovered:
                register(trade.plan)
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
            connection.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, 'ENTRY_PENDING', 0, '{}', ?, ?)",
                (plan.trade_id, plan.assignment_id, plan.instrument_id, plan.signal_id, plan.side,
                 json.dumps(_plan_payload(plan), sort_keys=True),
                 json.dumps(_profile_payload(plan), sort_keys=True), now, now),
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
                    "INSERT INTO targets VALUES (?, ?, ?, ?, 0, 0, 'PENDING')",
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

    def submit_action(
        self, action: TradeAction, *, assignment_id: str | None = None, trace=None
    ) -> bool:
        """Validate and durably queue one management action before broker delivery."""
        now = datetime.now(timezone.utc).isoformat()
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
            self._insert_action(connection, action, now)
            if isinstance(action, MoveStop):
                connection.execute(
                    "UPDATE protection SET pending_stop=?, pending_command_id=?, updated_at=? WHERE trade_id=?",
                    (str(action.stop_price), action.command_id, now, action.trade_id),
                )
            if trace is not None:
                self._traces.record_in_transaction(connection, trace)
        return True

    def _record_slippage_stub(self, decision, instrument, timeframe) -> None:
        trace = calculation_trace(
            "execution.slippage_guard",
            inputs={
                "requested_price": MeasuredValue(decision.price, "price"),
                "allowed_slippage": MeasuredValue(self._slippage, "RUB"),
                "instrument": MeasuredValue(instrument.ticker, "instrument-id"),
                "timeframe": MeasuredValue(timeframe, "timeframe"),
            },
            result=MeasuredValue(True, "accepted"),
            reason="stub-accepted-no-enforcement",
            formula="observation-only slippage guard stub",
            links=TraceLinks(signal_id=decision.event_id),
        )
        self._traces.record(trace)

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

    def consume(self, event: ExecutionEvent) -> bool:
        """Reduce one broker event, then schedule post-fill risk/protection checks."""
        applied = self._reducer.apply(event)
        if not applied:
            return False
        if event.status is ExecutionStatus.ACK:
            self._confirm_stop(event)
        if event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL} and self._post_fill_check:
            for action in self._post_fill_check(event):
                self.submit_action(action)
        return True

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
        try:
            recovered = self.restore()
            owned = self._owned_trade(recovered, assignment.id)
            if owned is not None:
                actions = self._manage_owned(owned, assignment, decision, instrument, frame, context, meta, timeframe)
                return SignalAdmission(actions=actions)
            return self._plan_entry(assignment, decision, instrument, frame, context, meta, timeframe)
        except Exception as exc:
            log.warning("actions_for_signal %s: %s", instrument.ticker, exc, exc_info=True)
            return SignalAdmission(
                rejections=(rejection_reason("admission-error", message=f"Ошибка при допуске сигнала: {exc}"),),
            )

    def manage(
        self,
        instrument,
        assignments,
        frame,
        context=None,
        *,
        timeframe: str | None = None,
    ) -> tuple[TradeAction, ...]:
        """Protect each live trade of the instrument from the provided frame."""
        contract = getattr(self._broker, "contract_for", None)
        meta = contract(instrument.ticker) if callable(contract) else None
        if meta is None:
            return ()
        results: list[TradeAction] = []
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
            try:
                profile = profile_cls()
                result, management_trace = profile.manage_with_trace(ManagementContext(plan, state, market))
            except Exception as exc:
                log.warning("manage %s: %s", plan.trade_id, exc)
                continue
            for action in result.actions:
                if isinstance(action, AddToTrade):
                    continue
                try:
                    if self.submit_action(action, assignment_id=plan.assignment_id, trace=management_trace):
                        results.append(action)
                except ValueError as exc:
                    log.debug("manage %s пропустил %s: %s", plan.trade_id, type(action).__name__, exc)
        return tuple(results)

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
            actions = tuple(
                action for action in management_result.actions
                if isinstance(action, AddToTrade)
            )
        self._record_slippage_stub(decision, instrument, timeframe)
        submitted: list[TradeAction] = []
        for action in actions:
            try:
                if self.submit_action(action, assignment_id=assignment.id, trace=management_trace):
                    submitted.append(action)
            except (ValueError, KeyError, TypeError) as exc:
                log.debug("actions_for_signal %s пропустил %s: %s", plan.trade_id, type(action).__name__, exc)
        return tuple(submitted)

    def _plan_entry(self, assignment, decision, instrument, frame, context, meta, timeframe) -> SignalAdmission:
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
            return SignalAdmission(
                rejections=(rejection_reason(code),),
            )
        quantity, risk_amount = self._size_open_quantity(plan, meta)
        if quantity <= 0:
            return SignalAdmission(
                rejections=(rejection_reason("zero-quantity"),),
            )
        action = OpenTrade(
            command_id=f"{plan.trade_id}:entry",
            trade_id=plan.trade_id,
            state_revision=0,
            reason="profile-entry",
            quantity=quantity,
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
        budget = self._budget_base()
        sizing_trace = calculation_trace(
            "portfolio.position_sizing",
            inputs={
                "risk_budget": MeasuredValue(budget * self._risk_limits.per_trade / Decimal("100"), "RUB"),
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
            risk_budget=budget * self._risk_limits.per_trade / Decimal("100"),
            margin_budget=budget,
            traces=(plan_trace, sizing_trace),
        )
        if accepted:
            return SignalAdmission(actions=(action,))
        return SignalAdmission(
            rejections=(rejection_reason("duplicate-signal"),),
        )

    def _size_open_quantity(self, plan: TradePlan, meta) -> tuple[int, Decimal]:
        """Size a fresh entry within per-trade/instrument/portfolio risk and margin."""
        value_per_point = Decimal(str(meta.step_cost)) / Decimal(str(meta.price_step))
        direction = Decimal("1") if plan.side == "BUY" else Decimal("-1")
        per_unit_risk = direction * (plan.reference_entry - plan.stop_price) * value_per_point
        if per_unit_risk <= 0:
            return 0, Decimal("0")
        budget = self._budget_base()
        existing = self._risk_trades()
        go = Decimal(str(meta.go_buy if plan.side == "BUY" else meta.go_sell))
        limits = self._risk_limits

        def allowed(quantity: int) -> bool:
            if go > 0 and go * quantity > budget:
                return False
            candidate = RiskTrade(
                trade_id=plan.trade_id,
                instrument_id=plan.instrument_id,
                groups=frozenset(),
                side=plan.side,
                quantity=quantity,
                average_price=plan.reference_entry,
                stop_price=plan.stop_price,
                price_step=Decimal(str(meta.price_step)),
                step_cost=Decimal(str(meta.step_cost)),
            )
            return self._risk.evaluate(
                balance=budget, equity=budget, limits=limits,
                trades=existing + (candidate,),
            ).allowed

        low, high = 0, self._max_qty
        while low < high:
            candidate_qty = (low + high + 1) // 2
            if allowed(candidate_qty):
                low = candidate_qty
            else:
                high = candidate_qty - 1
        if low <= 0:
            return 0, Decimal("0")
        return low, per_unit_risk * low

    def _budget_base(self) -> Decimal:
        row = self._storage.connection.execute(
            "SELECT balance, equity FROM account WHERE account_id = 1"
        ).fetchone()
        if row is None:
            return self._initial_balance
        return min(Decimal(row[0]), Decimal(row[1]))

    def _risk_trades(self) -> tuple[RiskTrade, ...]:
        contract_for = getattr(self._broker, "contract_for", None)
        trades: list[RiskTrade] = []
        for recovered in self.restore():
            plan, state = recovered.plan, recovered.state
            if state.quantity <= 0 or state.average_price is None:
                continue
            meta = contract_for(plan.instrument_id) if callable(contract_for) else None
            if meta is None or meta.price_step <= 0 or meta.step_cost <= 0:
                continue
            row = self._storage.connection.execute(
                "SELECT realized_pnl, fees FROM positions WHERE trade_id = ?", (plan.trade_id,)
            ).fetchone()
            realized_pnl = Decimal(row[0]) if row else Decimal("0")
            paid_fees = Decimal(row[1]) if row else Decimal("0")
            stop = state.confirmed_stop or plan.stop_price
            if (plan.side == "BUY") != (stop < state.average_price):
                stop = plan.stop_price
            trades.append(RiskTrade(
                trade_id=plan.trade_id,
                instrument_id=plan.instrument_id,
                groups=frozenset(),
                side=plan.side,
                quantity=state.quantity,
                average_price=state.average_price,
                stop_price=stop,
                price_step=Decimal(str(meta.price_step)),
                step_cost=Decimal(str(meta.step_cost)),
                paid_fees=paid_fees,
                realized_pnl=realized_pnl,
            ))
        return tuple(trades)

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
        requested_price = str(action.stop_price) if isinstance(action, MoveStop) else None
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
        rows = connection.execute(
            "SELECT risk_amount, margin_amount FROM reservations WHERE status = 'ACTIVE'"
        ).fetchall()
        used_risk = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        used_margin = sum((Decimal(row[1]) for row in rows), Decimal("0"))
        if used_risk + candidate.risk_amount > risk_budget or used_margin + candidate.margin_amount > margin_budget:
            raise ValueError("risk-or-margin-budget")
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

    def _confirm_stop(self, event: ExecutionEvent) -> None:
        with self._storage.transaction() as connection:
            row = connection.execute(
                "SELECT requested_price FROM orders WHERE command_id = ? AND action_type = 'MOVESTOP'",
                (event.command_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE protection SET confirmed_stop=?, pending_stop=NULL, confirmed_order_id=?, "
                    "pending_command_id=NULL, updated_at=? WHERE trade_id=?",
                    (row[0], event.order_id, event.timestamp.isoformat(), event.trade_id),
                )
                connection.execute(
                    "UPDATE trades SET state_revision = state_revision + 1, updated_at = ? WHERE trade_id = ?",
                    (event.timestamp.isoformat(), event.trade_id),
                )


def _profile_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "name": plan.profile.name,
        "version": plan.profile.version,
        "parameters": _json_value(dict(plan.profile.parameters)),
    }


def _plan_payload(plan: TradePlan) -> dict[str, object]:
    return {
        "reference_entry": str(plan.reference_entry), "stop_price": str(plan.stop_price),
        "targets": [{"target_id": target.target_id, "share": str(target.share)} for target in plan.targets],
    }


def _action_payload(action: TradeAction) -> dict[str, object]:
    result = asdict(action)
    result["type"] = type(action).__name__
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in result.items()}


def _action_from_payload(payload: dict[str, object]) -> TradeAction:
    data = dict(payload)
    action_type = data.pop("type")
    if action_type == "MoveStop":
        data["stop_price"] = Decimal(str(data["stop_price"]))
    constructors = {
        "OpenTrade": OpenTrade, "AddToTrade": AddToTrade, "ReduceTrade": ReduceTrade,
        "CloseTrade": CloseTrade, "MoveStop": MoveStop, "CancelEntry": CancelEntry,
    }
    try:
        return constructors[str(action_type)](**data)
    except KeyError as error:
        raise ValueError(f"unknown action type {action_type!r}") from error


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
