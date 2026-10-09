"""Защитные закрытия адресных сделок докладываются исполнением (change emit-protective-close-events)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.broker import JournalBroker
from src.broker.port import ExecutionStatus
from src.portfolio import PositionManager
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def _plan(trade_id: str = "trade-1") -> TradePlan:
    return TradePlan(
        trade_id=trade_id,
        assignment_id="assignment-1",
        instrument_id="NG",
        side="BUY",
        signal_id=f"signal-{trade_id}",
        reference_entry=Decimal("100"),
        stop_price=Decimal("98"),
        targets=(TargetPlan("tp-1", Decimal("102"), Decimal("0.5")),
                 TargetPlan("tp-2", Decimal("104"), Decimal("0.5"))),
        profile=ProfileSnapshot("levels_rr", "1", {}),
        created_at=NOW,
    )


def _broker() -> JournalBroker:
    broker = JournalBroker(None, PositionManager(initial_deposit=100_000, max_risk_pct=2), ["14:05"])
    broker.set_names({"NG": "NG-10.26"})
    return broker


def _env(path):
    broker = _broker()
    manager = TradeManager(Storage(path), broker, initial_balance=Decimal("10000"))
    return broker, manager


def _open_entry(manager: TradeManager, plan: TradePlan, quantity: int = 5, now=NOW) -> None:
    assert manager.submit_plan(plan, OpenTrade(f"{plan.trade_id}:entry", plan.trade_id, 0, "profile-entry", quantity))
    manager.dispatch(now)


def _bar(broker: JournalBroker, manager: TradeManager | None, minute: int,
         open_: float, low: float, high: float, close: float) -> list:
    """Подать закрытый бар; consume событий — только при manager."""
    broker.track_bar(NOW + timedelta(minutes=minute), {"NG": (open_, low, high, close)}, {})
    events = list(broker.drain_addressed_events())
    if manager is not None:
        for event in events:
            manager.consume(event)
    return events


def _close_asserts(storage, trade_id="trade-1"):
    assert storage.connection.execute("SELECT phase FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()[0] == "CLOSED"
    return storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]


def test_protective_stop_is_reported_and_reducer_closes_trade(tmp_path):
    broker, manager = _env(tmp_path / "trades.sqlite3")
    plan = _plan()
    _open_entry(manager, plan)
    _bar(broker, manager, 1, 100, 99, 101, 100)
    assert manager.restore()[0].state.phase.value == "OPEN"

    _bar(broker, manager, 2, 97, 96, 105, 98)  # гэп-открытие хуже стопа → исполнение по 97

    assert _close_asserts(manager._storage) == 2  # вход + защитный стоп
    stop_fill = manager._storage.connection.execute(
        "SELECT f.quantity, f.price, f.fee, o.action_type FROM fills f "
        "JOIN orders o ON o.order_id = f.order_id WHERE o.action_type = 'STOP'"
    ).fetchone()
    assert stop_fill == (5, "97", "0", "STOP")
    pv = manager._storage.connection.execute(
        "SELECT o.status, ord.action_type FROM outbox o "
        "JOIN orders ord ON ord.command_id = o.command_id WHERE o.command_id LIKE '%:pv:stop:%'"
    ).fetchone()
    assert pv == ("SENT", "STOP")
    assert not broker.manager.positions
    assert broker.trade_state(plan.trade_id).confirmed_stop is None


def test_target_fill_is_reported_binds_planned_quantity_and_keeps_remainder(tmp_path):
    broker, manager = _env(tmp_path / "trades.sqlite3")
    plan = _plan()
    _open_entry(manager, plan, quantity=5)
    _bar(broker, manager, 1, 100, 99, 101, 100)  # вход 5 лотов
    _bar(broker, manager, 2, 101, 100, 102, 101)  # первая цель 102 → 2 лота из 5

    target = manager._storage.connection.execute(
        "SELECT planned_quantity, filled_quantity, status FROM targets WHERE trade_id = ? AND target_id = 'tp-1'",
        (plan.trade_id,),
    ).fetchone()
    assert target == (2, 2, "FILLED")
    assert manager._storage.connection.execute(
        "SELECT phase FROM trades WHERE trade_id = ?", (plan.trade_id,)
    ).fetchone()[0] == "REDUCING"
    assert broker.manager.positions[plan.trade_id].qty == 3

    _bar(broker, manager, 3, 99, 96, 100, 97)  # стоп по остатку 3 лотов

    assert _close_asserts(manager._storage) == 3  # вход + цель + стоп
    target_planned = manager._storage.connection.execute(
        "SELECT planned_quantity, filled_quantity, status FROM targets "
        "WHERE trade_id = ? AND target_id = 'tp-2'", (plan.trade_id,)
    ).fetchone()
    assert target_planned == (0, 0, "PENDING")  # tp-2 ещё не заполнялся: план привяжется на его филле


def test_protective_event_is_idempotent_when_redelivered(tmp_path):
    broker, manager = _env(tmp_path / "trades.sqlite3")
    plan = _plan()
    _open_entry(manager, plan)
    _bar(broker, manager, 1, 100, 99, 101, 100)
    _bar(broker, manager, 2, 101, 100, 102, 101)
    events = _bar(broker, None, 3, 97, 96, 105, 98)
    assert [e.reason for e in events] == ["protective"]
    stop_event = events[0]

    assert manager.consume(stop_event) is True
    assert manager.consume(stop_event) is False  # повторная пересылка идемпотентна
    assert _close_asserts(manager._storage) == 3


def test_synthetic_outbox_status_sent_is_never_claimed_for_dispatch(tmp_path):
    broker, manager = _env(tmp_path / "trades.sqlite3")
    _open_entry(manager, _plan())
    _bar(broker, manager, 1, 100, 99, 101, 100)
    _bar(broker, manager, 2, 97, 96, 105, 98)

    pv_rows = manager._storage.connection.execute(
        "SELECT command_id FROM outbox WHERE status = 'SENT' AND command_id LIKE '%:pv:%'"
    ).fetchall()
    assert pv_rows
    assert manager._storage.claim_outbox(100) == ()


def test_sequential_improving_stops_are_not_rejected_as_stale(tmp_path):
    """Повторные улучшающие стопы не отклоняются stale-state-revision.

    Ревизии брокера и durable-БД повышаются синхронно: БД — при приёме ACK
    (``_confirm_stop``), брокер — там же, а не при исполнении на следующем баре.
    """
    broker, manager = _env(tmp_path / "trades.sqlite3")
    plan = _plan()
    _open_entry(manager, plan)
    _bar(broker, manager, 1, 100, 99, 101, 100)  # вход исполнен (rev 1)

    for minute, stop in enumerate((98.4, 98.6, 98.8), start=2):
        revision = manager.restore()[0].state.state_revision
        assert manager.submit_action(MoveStop(
            f"{plan.trade_id}:ma-cloud-stop:{minute}", plan.trade_id, revision,
            "ma-cloud-protective-stop", Decimal(str(stop)),
        ))
        (event,) = manager.dispatch(NOW + timedelta(minutes=minute))
        assert event.status is ExecutionStatus.ACK, event.reason
        assert event.reason == "next-bar"
        assert broker.trade_state(plan.trade_id).revision == manager.restore()[0].state.state_revision

    _bar(broker, manager, 5, 97, 96, 105, 98)  # отложенные стопы исполняются, затем защитный стоп
    assert _close_asserts(manager._storage) == 2
    assert broker.trade_state(plan.trade_id).confirmed_stop is None


def test_restart_resumes_protection_and_stop_is_reported_after_restart(tmp_path):
    database = tmp_path / "trades.sqlite3"
    broker, manager = _env(database)
    plan = _plan()
    _open_entry(manager, plan, quantity=5)
    _bar(broker, manager, 1, 100, 99, 101, 100)  # вход 5 лотов
    _bar(broker, manager, 2, 101, 100, 102, 101)  # tp-1: 2 из 5
    manager._storage.close()

    broker_after, manager_after = _env(database)
    first, = manager_after.restore()
    assert first.target_filled == {"tp-1": 2, "tp-2": 0}
    trade = broker_after.trade_state(plan.trade_id)
    assert trade is not None
    assert trade.revision == first.state.state_revision
    assert trade.entry_quantity == 5
    assert trade.confirmed_stop == plan.stop_price
    assert broker_after.manager.positions[plan.trade_id].qty == 3
    assert trade.target_filled["tp-1"] == 2

    manager_after.restore()  # повторный вызов не перезаписывает живое состояние
    assert broker_after.trade_state(plan.trade_id).revision == first.state.state_revision

    _bar(broker_after, manager_after, 3, 97, 96, 105, 98)
    assert _close_asserts(manager_after._storage) == 3
    assert not broker_after.manager.positions