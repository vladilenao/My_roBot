"""Граница защитного стопа относительно рынка (invariants of submit_action).

Стоп по позиции, уже ушедшей за него, исполняется сразу по худшей цене и
усиливает убыток вместо защиты. Проверка живёт в ``submit_action`` — единой
точке отправки защитного действия, поэтому результат не зависит от профиля.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop, OpenTrade
from src.trade_management.errors import InvalidStopBoundaryError
from src.trade_management.manager import STOP_BOUNDARY_REASON, TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

PROFILES = ("levels_rr", "pattern_targets", "ma_cloud", "atr_trend")


class FillingBroker(BrokerPort):
    def __init__(self, meta=None) -> None:
        self.meta = meta

    def contract_for(self, ticker: str):
        return self.meta

    def submit(self, action, now):
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


def _plan(side: str = "BUY", profile: str = "levels_rr") -> TradePlan:
    stop, target = (Decimal("96"), Decimal("104")) if side == "BUY" else (Decimal("104"), Decimal("96"))
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", side, "signal-1", Decimal("100"), stop,
        (TargetPlan("tp-1", target, Decimal("1")),),
        ProfileSnapshot(profile, "1", {"buffer": Decimal("1")}), NOW,
    )


class ContractMetaStub:
    """Метаданные контракта без даты экспирации: ``manage`` их требует."""

    ticker = "NGV6"
    price_step = 1.0
    step_cost = 100.0
    go_buy = 5000.0
    go_sell = 5000.0
    expiration_date = None


def _opened(
    storage: Storage, side: str = "BUY", profile: str = "levels_rr", *, with_contract: bool = False
) -> TradeManager:
    broker = FillingBroker(ContractMetaStub() if with_contract else None)
    manager = TradeManager(storage, broker, initial_balance=Decimal("1000"))
    manager.submit_plan(_plan(side, profile), OpenTrade("open-1", "trade-1", 0, "entry", 2))
    manager.dispatch(NOW)
    return manager


def _outbox_count(storage: Storage) -> int:
    return storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


def _traces(storage: Storage) -> list[tuple[str, str]]:
    return [
        (row[0], row[1]) for row in storage.connection.execute(
            "SELECT algorithm, reason FROM calculations ORDER BY created_at, calculation_id"
        )
    ]


def test_long_stop_at_or_above_market_is_rejected(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105")),
                market_close=Decimal("104"),
            )

        assert _outbox_count(storage) == 1  # только исходный вход


def test_short_stop_at_or_below_market_is_rejected(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "SELL")

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("95")),
                market_close=Decimal("96"),
            )


def test_stop_exactly_at_market_is_rejected(tmp_path):
    """Равенство недопустимо: стоп на текущей цене сработает на том же баре."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("100")),
                market_close=Decimal("100"),
            )


def test_rejection_writes_no_command_and_records_reason(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")
        before = _outbox_count(storage)

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105")),
                market_close=Decimal("104"),
            )

        assert _outbox_count(storage) == before
        assert ("management.stop-boundary", STOP_BOUNDARY_REASON) in _traces(storage)
        assert storage.connection.execute(
            "SELECT reason FROM calculations WHERE algorithm = 'management.stop-boundary'"
        ).fetchone()[0] == STOP_BOUNDARY_REASON


def test_rejection_trace_survives_rollback_of_the_step(tmp_path):
    """След коммится до raise, поэтому переживает откат шага транзакции."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105")),
                market_close=Decimal("104"),
            )

        storage.connection.execute("BEGIN IMMEDIATE")
        storage.connection.rollback()

        assert ("management.stop-boundary", STOP_BOUNDARY_REASON) in _traces(storage)


def test_rejection_is_distinguishable_from_duplicate_command(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")
        rejected = MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105"))
        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(rejected, market_close=Decimal("104"))

        applied = MoveStop("stop-2", "trade-1", 1, "break-even", Decimal("98"))
        assert manager.submit_action(applied, market_close=Decimal("104")) is True
        duplicate = manager.submit_action(applied, market_close=Decimal("104"))

        assert duplicate is False


def test_valid_stop_is_applied(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        assert manager.submit_action(
            MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("103.5")),
            market_close=Decimal("104"),
        ) is True
        assert storage.connection.execute(
            "SELECT pending_stop FROM protection WHERE trade_id = 'trade-1'"
        ).fetchone()[0] == "103.5"


def test_missing_market_price_skips_check_and_warns(tmp_path, caplog):
    """Fail-safe open: пустой фрейм не должен заморозить аварийный стоп."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        with caplog.at_level("WARNING"):
            assert manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105"))
            ) is True

        assert "trade-1" in caplog.text
        assert storage.connection.execute(
            "SELECT pending_stop FROM protection WHERE trade_id = 'trade-1'"
        ).fetchone()[0] == "105"
        assert STOP_BOUNDARY_REASON not in caplog.text


def test_non_protective_action_is_not_checked(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY")

        assert manager.submit_action(
            MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105")),
            assignment_id="assignment-1",
            market_close=None,
        ) is True


@pytest.mark.parametrize("profile", PROFILES)
def test_every_profile_is_bound_by_the_same_rule(tmp_path, profile):
    with Storage(tmp_path / f"{profile}.sqlite3") as storage:
        manager = _opened(storage, "BUY", profile)

        with pytest.raises(InvalidStopBoundaryError):
            manager.submit_action(
                MoveStop("stop-1", "trade-1", 1, "break-even", Decimal("105")),
                market_close=Decimal("104"),
            )


def test_one_rejected_action_does_not_block_the_other_in_the_same_pass(tmp_path, monkeypatch):
    """Отказ прерывает только своё действие, сопровождение инструмента идёт дальше."""
    from src.trade_management import manager as manager_module
    from src.trade_management.profiles.base import ManagementContext, ProfileResult

    class TwoStopProfile:
        NAME = "two_stop"

        def manage_with_trace(self, context: ManagementContext):
            state = context.state
            actions = (
                MoveStop("stop-bad", context.plan.trade_id, state.state_revision,
                         "break-even", Decimal("105")),
                MoveStop("stop-good", context.plan.trade_id, state.state_revision,
                         "break-even", Decimal("103.5")),
            )
            return ProfileResult(actions=actions, state=state), None

    monkeypatch.setitem(manager_module.PROFILE_CLASSES, "two_stop", TwoStopProfile)
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _opened(storage, "BUY", "two_stop", with_contract=True)
        frame = pd.DataFrame(
            {
                "datetime": [NOW],
                "open": [Decimal("104")], "high": [Decimal("105")],
                "low": [Decimal("103")], "close": [Decimal("104")], "volume": [1],
            }
        )
        instrument = type("Instrument", (), {"ticker": "NGV6"})()

        submitted = manager.manage(instrument, (), frame, now=NOW)

        assert [action.command_id for action in submitted] == ["stop-good"]
        assert storage.connection.execute(
            "SELECT pending_stop FROM protection WHERE trade_id = 'trade-1'"
        ).fetchone()[0] == "103.5"
        assert ("management.stop-boundary", STOP_BOUNDARY_REASON) in _traces(storage)