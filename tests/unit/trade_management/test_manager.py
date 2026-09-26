from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import json
import sqlite3
import pytest

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.market_context.models import MarketContext, TrendDirection, TrendResult
from src.portfolio.models import ContractMeta
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import AddToTrade, CancelEntry, CloseTrade, MoveStop, OpenTrade
from src.trade_management.manager import TradeManager, _entry_ttl_seconds
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePhase, TradePlan
import pandas as pd


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeBroker(BrokerPort):
    def __init__(self) -> None:
        self.actions = []
        self.plans = []

    def register_trade(self, plan: TradePlan) -> None:
        self.plans.append(plan)

    def submit(self, action, now):
        self.actions.append(action)
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


class ContractBroker(FakeBroker):
    """Брокер с ``contract_for`` и честным учётом наполнения позиций.

    Входы исполняются (FILL), отмены возвращаются REJECT-классом ``CANCEL``,
    закрытия списывают оставшийся объём.
    """

    def __init__(self, meta: ContractMeta | None) -> None:
        super().__init__()
        self.meta = meta
        self.quantities: dict[str, int] = {}

    def contract_for(self, ticker: str) -> ContractMeta | None:
        return self.meta

    def submit(self, action, now):
        self.actions.append(action)
        if isinstance(action, (OpenTrade, AddToTrade)):
            self.quantities[action.trade_id] = self.quantities.get(action.trade_id, 0) + action.quantity
            filled, status = action.quantity, ExecutionStatus.FILL
        elif isinstance(action, CancelEntry):
            filled, status = 0, ExecutionStatus.CANCEL
        else:
            filled = self.quantities.get(action.trade_id, 0)
            self.quantities[action.trade_id] = 0
            status = ExecutionStatus.FILL
        return ExecutionEvent(
            f"{action.command_id}:{status.value}", action.command_id, action.command_id, action.trade_id,
            status, filled, Decimal("100") if filled else None, Decimal("0"), now, action.reason,
        )


class StaleEntryBroker(FakeBroker):
    """Брокер, чей вход принимается (ACK), но не исполняется; отмены срабатывают."""

    def __init__(self, meta: ContractMeta | None) -> None:
        super().__init__()
        self.meta = meta

    def contract_for(self, ticker: str) -> ContractMeta | None:
        return self.meta

    def submit(self, action, now):
        self.actions.append(action)
        if isinstance(action, CancelEntry):
            filled, status = 0, ExecutionStatus.CANCEL
        else:
            filled, status = 0, ExecutionStatus.ACK
        return ExecutionEvent(
            f"{action.command_id}:{status.value}", action.command_id, action.command_id,
            action.trade_id, status, filled, None, Decimal("0"), now, action.reason,
        )


def _plan() -> TradePlan:
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW,
    )


def _plan_on(timeframe: str, ticker: str = "NGV6", trade_id: str = "trade-1") -> TradePlan:
    return TradePlan(
        trade_id, "assignment-1", ticker, "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), NOW, timeframe,
    )


def _contract(expiration_days: int | None) -> ContractMeta:
    """Контракт с датой экспирации через ``expiration_days`` суток (naive UTC)."""
    if expiration_days is None:
        expiration_date = None
    else:
        expiration_date = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=expiration_days)
    return ContractMeta(
        ticker="NGV6", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0,
        expiration_date=expiration_date,
    )


def test_manager_persists_plan_before_dispatches_and_reduces_fill(tmp_path):
    broker = FakeBroker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("1000"))
        assert manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2))
        assert storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

        events = manager.dispatch(NOW)

        assert events[0].status is ExecutionStatus.FILL
        recovered, = manager.restore()
        assert recovered.state.quantity == 2
        assert recovered.state.average_price == Decimal("100")
        assert broker.plans[-1].profile.parameters["buffer"] == "1"


def test_manager_rejects_wrong_owner_stale_revision_and_invalid_phase(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))

        with pytest.raises(ValueError, match="belong"):
            manager.submit_action(AddToTrade("add-1", "trade-1", 0, "add", 1), assignment_id="other")
        with pytest.raises(ValueError, match="invalid in phase"):
            manager.submit_action(AddToTrade("add-1", "trade-1", 0, "add", 1), assignment_id="assignment-1")

        manager.dispatch(NOW)
        with pytest.raises(ValueError, match="stale"):
            manager.submit_action(AddToTrade("add-2", "trade-1", 0, "add", 1))


def test_manager_restores_open_trade_without_current_configuration(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)

    broker = FakeBroker()
    with Storage(path) as storage:
        recovered, = TradeManager(storage, broker).restore()

    assert recovered.plan.assignment_id == "assignment-1"
    assert recovered.state.quantity == 1
    assert broker.plans == [recovered.plan]


def test_stop_ack_confirms_protection_and_advances_durable_revision(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, FakeBroker())
        manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)
        manager.submit_action(MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("100")))

        assert manager.consume(ExecutionEvent(
            "stop-1:ack", "stop-1", "stop-1", "trade-1", ExecutionStatus.ACK, 0, None,
            Decimal("0"), NOW, "confirmed",
        ))
        recovered, = manager.restore()

    assert recovered.state.confirmed_stop == Decimal("100")
    assert recovered.state.pending_stop is None
    assert recovered.state.state_revision == 2


def test_rejected_plan_is_recorded_as_reproducible_trace(tmp_path):
    """Отказ профиля (нет подтверждённого уровня) пишется в репозиторий расчётов."""
    frame = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-06-22 22:30:00"]),
        "open": [3.146], "high": [3.15], "low": [3.144], "close": [3.147],
    })
    context = MarketContext(TrendResult(TrendDirection.FLAT, 0.0), [], 3.147)
    decision = Decision(SignalType.SELL, 3.147, event_id="signal-1")
    assignment = SimpleNamespace(id="assignment-1", management="levels_rr", filter_profile="raw", timeframe="15m")
    instrument = SimpleNamespace(ticker="NGV6", base_code="NG")
    meta = ContractMeta(ticker="NGV6", price_step=0.001, step_cost=8.4, go_buy=1000.0, go_sell=1000.0)

    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, ContractBroker(meta), initial_balance=Decimal("100000"))
        admission = manager.actions_for_signal(assignment, decision, instrument, frame, context)

        assert [rejection.code for rejection in admission.rejections] == ["missing-structure"]

        row = storage.connection.execute(
            "SELECT outcome, reason, algorithm, output_json FROM calculations"
        ).fetchone()
        assert row is not None
        outcome, reason, algorithm, output = row
        assert (outcome, reason, algorithm) == ("REJECTED", "missing-structure", "profile.levels_rr.plan")
        assert json.loads(output) == {"unit": "trade-plan", "value": {"reason": "missing-structure"}}


class TestManageExpiringContract:
    def test_manage_cancels_entry_pending_when_contract_expiring(self, tmp_path):
        broker = ContractBroker(_contract(expiration_days=1))
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = TradeManager(storage, broker)
            manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1))

            actions = manager.manage(SimpleNamespace(ticker="NGV6"), [], None)

            (cancel,) = actions
            assert isinstance(cancel, CancelEntry)
            assert cancel.reason == "contract-expiring"
            assert cancel.trade_id == "trade-1"
            stored = storage.connection.execute(
                "SELECT phase FROM trades WHERE trade_id = ?", ("trade-1",)
            ).fetchone()
            assert stored[0] == TradePhase.ENTRY_PENDING.value

    def test_manage_closes_open_position_when_contract_expiring(self, tmp_path):
        broker = ContractBroker(_contract(expiration_days=1))
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = TradeManager(storage, broker, initial_balance=Decimal("1000"))
            manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2))
            manager.dispatch(NOW)

            actions = manager.manage(SimpleNamespace(ticker="NGV6"), [], None)

            (close,) = actions
            assert isinstance(close, CloseTrade)
            assert close.reason == "contract-expiring"
            assert close.trade_id == "trade-1"

    def test_manage_leaves_share_contract_without_expiration_unaffected(self, tmp_path):
        broker = ContractBroker(_contract(expiration_days=None))
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = TradeManager(storage, broker, initial_balance=Decimal("1000"))
            manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 2))
            manager.dispatch(NOW)

            actions = manager.manage(SimpleNamespace(ticker="NGV6"), [], None)

            assert not any(getattr(action, "reason", None) == "contract-expiring" for action in actions)
            recovered, = manager.restore()
            assert recovered.state.phase is TradePhase.OPEN
            assert recovered.state.quantity == 2


class TestManageStaleEntry:
    def _stale_manager(self, path, timeframe: str, *, dispatch_at, plan=None, initial_balance="1000"):
        plan = plan or _plan_on(timeframe)
        broker = StaleEntryBroker(_contract(expiration_days=None))
        manager = TradeManager(path, broker, initial_balance=Decimal(initial_balance))
        manager.submit_plan(plan, OpenTrade("open-1", plan.trade_id, 0, "entry", 2))
        if dispatch_at is not None:
            manager.dispatch(dispatch_at)
        return manager

    def test_cancels_stale_acked_entry_with_entry_timeout(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "5m", dispatch_at=NOW)
            (cancel,) = manager.manage(SimpleNamespace(ticker="NGV6"), [], None, now=NOW + timedelta(seconds=3601))

            assert isinstance(cancel, CancelEntry)
            assert cancel.reason == "entry-timeout"
            assert cancel.trade_id == "trade-1"

    def test_keeps_fresh_acked_entry_within_ttl(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "5m", dispatch_at=NOW)
            actions = manager.manage(SimpleNamespace(ticker="NGV6"), [], None, now=NOW + timedelta(seconds=3599))

            assert not any(getattr(action, "reason", None) == "entry-timeout" for action in actions)
            recovered, = manager.restore()
            assert recovered.state.phase is TradePhase.ENTRY_PENDING

    def test_skips_entry_without_confirmed_order(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "5m", dispatch_at=None)
            actions = manager.manage(
                SimpleNamespace(ticker="NGV6"), [], None,
                now=NOW + timedelta(days=7),
            )

            assert not any(getattr(action, "reason", None) == "entry-timeout" for action in actions)

    def test_ignores_stale_entry_on_other_instrument(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "5m", dispatch_at=NOW, plan=_plan_on("5m", ticker="BRV6"))
            actions = manager.manage(
                SimpleNamespace(ticker="NGV6"), [],
                None, now=NOW + timedelta(seconds=3601),
            )

            assert not any(getattr(action, "reason", None) == "entry-timeout" for action in actions)

    def test_dispatch_cancels_stale_entry_to_cancelled_phase(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "5m", dispatch_at=NOW)
            manager.manage(SimpleNamespace(ticker="NGV6"), [], None, now=NOW + timedelta(seconds=3601))
            manager.dispatch(NOW + timedelta(seconds=3601))

            recovered, = storage.load_trades(include_terminal=True)
            assert recovered.state.phase is TradePhase.CANCELLED

    def test_legacy_plan_without_timeframe_uses_24h_backstop(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = self._stale_manager(storage, "", dispatch_at=NOW)
            old_actions = manager.manage(
                SimpleNamespace(ticker="NGV6"), [], None, now=NOW + timedelta(hours=25),
            )
            (cancel,) = old_actions
            assert isinstance(cancel, CancelEntry)
            assert cancel.reason == "entry-timeout"

    def test_cancelled_stale_entry_releases_its_reservation(self, tmp_path):
        plan = _plan_on("5m")
        reservation = ReservationCandidate(
            reservation_id=f"reservation:{plan.trade_id}", trade_id=plan.trade_id,
            order_id="open-1", priority=0, assignment_id=plan.assignment_id,
            instrument_id=plan.instrument_id, signal_id=plan.signal_id,
            risk_amount=Decimal("800"), margin_amount=Decimal("10000"),
        )
        with Storage(tmp_path / "trades.sqlite3") as storage:
            broker = StaleEntryBroker(_contract(expiration_days=None))
            manager = TradeManager(storage, broker, initial_balance=Decimal("100000"))
            manager.submit_plan(
                plan, OpenTrade("open-1", plan.trade_id, 0, "entry", 2),
                reservation=reservation,
                risk_budget=Decimal("2000"), margin_budget=Decimal("100000"),
            )
            manager.dispatch(NOW)
            assert _active_reserved_risk(storage) == [Decimal("800")]

            manager.manage(SimpleNamespace(ticker="NGV6"), [], None, now=NOW + timedelta(seconds=3601))
            manager.dispatch(NOW + timedelta(seconds=3601))

            assert _active_reserved_risk(storage) == []

    def test_pending_entry_keeps_its_reservation(self, tmp_path):
        plan = _plan_on("5m")
        reservation = ReservationCandidate(
            reservation_id=f"reservation:{plan.trade_id}", trade_id=plan.trade_id,
            order_id="open-1", priority=0, assignment_id=plan.assignment_id,
            instrument_id=plan.instrument_id, signal_id=plan.signal_id,
            risk_amount=Decimal("800"), margin_amount=Decimal("10000"),
        )
        with Storage(tmp_path / "trades.sqlite3") as storage:
            broker = StaleEntryBroker(_contract(expiration_days=None))
            manager = TradeManager(storage, broker, initial_balance=Decimal("100000"))
            manager.submit_plan(
                plan, OpenTrade("open-1", plan.trade_id, 0, "entry", 2),
                reservation=reservation,
                risk_budget=Decimal("2000"), margin_budget=Decimal("100000"),
            )
            manager.dispatch(NOW)

            assert _active_reserved_risk(storage) == [Decimal("800")]


def _active_reserved_risk(storage: Storage) -> list[Decimal]:
    return [
        Decimal(row[0]) for row in storage.connection.execute(
            "SELECT risk_amount FROM reservations WHERE status = 'ACTIVE' ORDER BY reservation_id"
        ).fetchall()
    ]


def test_entry_ttl_seconds_follows_timeframe():
    assert _entry_ttl_seconds("") == 24 * 3600
    assert _entry_ttl_seconds("5m") == 3600
    assert _entry_ttl_seconds("1h") == 4 * 3600
    assert _entry_ttl_seconds("1d") == 24 * 3600


def test_integrity_error_on_admission_is_russian_db_message(tmp_path, monkeypatch):
    frame = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-06-22 22:30:00"]),
        "open": [3.146], "high": [3.15], "low": [3.144], "close": [3.147],
    })
    context = MarketContext(TrendResult(TrendDirection.FLAT, 0.0), [], 3.147)
    decision = Decision(SignalType.BUY, 3.147, event_id="signal-1")
    assignment = SimpleNamespace(id="assignment-1", management="levels_rr", filter_profile="raw", timeframe="15m")
    instrument = SimpleNamespace(ticker="NGV6", base_code="NG")
    meta = ContractMeta(ticker="NGV6", price_step=0.001, step_cost=8.4, go_buy=1000.0, go_sell=1000.0)

    def _integrity_failure(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: targets.target_id")

    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, ContractBroker(meta), initial_balance=Decimal("100000"))
        monkeypatch.setattr(manager, "_plan_entry", _integrity_failure)
        admission = manager.actions_for_signal(assignment, decision, instrument, frame, context)

        (rejection,) = admission.rejections
        assert rejection.code == "admission-error"
        assert rejection.message == (
            "Ошибка при допуске сигнала: Ошибка на уровне БД: нарушение целостности данных"
        )
