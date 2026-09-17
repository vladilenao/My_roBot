from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker import ExecutionEvent, ExecutionStatus, JournalBroker
from src.portfolio import PositionManager
from src.trade_journal import TradeJournal
from src.trade_management import (
    CancelEntry,
    CloseTrade,
    MoveStop,
    OpenTrade,
    ProfileSnapshot,
    ReduceTrade,
    TargetPlan,
    TradePlan,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def test_execution_event_models_fill_and_partial_outcomes():
    event = ExecutionEvent(
        execution_id="fill-1",
        order_id="order-1",
        command_id="command-1",
        trade_id="trade-1",
        status=ExecutionStatus.PARTIAL,
        filled_quantity=2,
        price=Decimal("101.25"),
        fee=Decimal("1.50"),
        timestamp=NOW,
        reason="limit-reached",
    )

    assert event.status is ExecutionStatus.PARTIAL
    assert event.filled_quantity == 2


@pytest.mark.parametrize("status", [ExecutionStatus.ACK, ExecutionStatus.REJECT, ExecutionStatus.CANCEL])
def test_non_fill_outcomes_preserve_a_reason(status):
    event = ExecutionEvent(
        execution_id="outcome-1",
        order_id="order-1",
        command_id="command-1",
        trade_id="trade-1",
        status=status,
        filled_quantity=0,
        price=None,
        fee=Decimal("0"),
        timestamp=NOW,
        reason="risk-recheck",
    )

    assert event.reason == "risk-recheck"


@pytest.mark.parametrize("status", [ExecutionStatus.FILL, ExecutionStatus.PARTIAL])
def test_fill_outcomes_require_quantity_and_price(status):
    with pytest.raises(ValueError, match="quantity and price"):
        ExecutionEvent(
            execution_id="fill-1",
            order_id="order-1",
            command_id="command-1",
            trade_id="trade-1",
            status=status,
            filled_quantity=0,
            price=None,
            fee=Decimal("0"),
            timestamp=NOW,
            reason="limit-reached",
        )


def _plan(trade_id: str = "trade-NG-a") -> TradePlan:
    return TradePlan(
        trade_id=trade_id,
        assignment_id=f"assignment-{trade_id}",
        instrument_id="NG",
        side="BUY",
        signal_id=f"signal-{trade_id}",
        reference_entry=Decimal("100"),
        stop_price=Decimal("98"),
        targets=(TargetPlan("target-1", Decimal("102"), Decimal("0.5")), TargetPlan("target-2", Decimal("104"), Decimal("0.5"))),
        profile=ProfileSnapshot("levels_rr", "1", {}),
        created_at=NOW,
    )


def _bar(broker: JournalBroker, now: datetime, open_: float, low: float, high: float, close: float) -> None:
    broker.track_bar(now, {"NG": (open_, low, high, close)}, {})


def test_journal_broker_executes_exact_trade_and_command_and_preserves_reason(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan())
    broker.submit(OpenTrade("command-open-7", "trade-NG-a", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100, 99, 101, 100)
    action = ReduceTrade("command-reduce-7", "trade-NG-a", 1, "target-1", 1, "target-1")

    event = broker.submit(action, NOW)

    assert event.execution_id == "command-reduce-7:fill"
    assert event.order_id == "command-reduce-7"
    assert event.command_id == "command-reduce-7"
    assert event.trade_id == "trade-NG-a"
    assert event.status is ExecutionStatus.FILL
    assert event.reason == "target-1"


def test_journal_broker_cancellation_preserves_reason_and_command_retry(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan("trade-NG-b"))
    action = CancelEntry("command-cancel-2", "trade-NG-b", 0, "risk-recheck")

    first = broker.submit(action, NOW)
    repeated = broker.submit(action, NOW)

    assert first.status is ExecutionStatus.CANCEL
    assert first.reason == "risk-recheck"
    assert repeated == first


def test_two_same_ticker_trades_keep_independent_remainders_and_targets(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan("trade-NG-a"))
    broker.register_trade(_plan("trade-NG-b"))
    broker.submit(OpenTrade("open-a", "trade-NG-a", 0, "entry", 3), NOW)
    broker.submit(OpenTrade("open-b", "trade-NG-b", 0, "entry", 2), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100, 99, 101, 100)

    result = broker.submit(ReduceTrade("reduce-a", "trade-NG-a", 1, "target-1", 1, "target-1"), NOW)

    assert result.status is ExecutionStatus.FILL
    assert broker.manager.positions["trade-NG-a"].qty == 2
    assert broker.manager.positions["trade-NG-b"].qty == 2
    assert broker.trade_state("trade-NG-a").target_filled == {"target-1": 1, "target-2": 0}
    assert broker.trade_state("trade-NG-b").target_filled == {"target-1": 0, "target-2": 0}


def test_stop_is_exposed_only_after_its_addressed_confirmation(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan())

    rejected = broker.submit(ReduceTrade("reduce-before-open", "trade-NG-a", 1, "target", 0), NOW)
    broker.submit(OpenTrade("open", "trade-NG-a", 0, "entry", 2), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100, 99, 101, 100)
    moved = broker.submit(MoveStop("move", "trade-NG-a", 1, "trail", Decimal("99")), NOW)
    _bar(broker, NOW + timedelta(minutes=2), 100, 99.5, 101, 100)

    assert rejected.status is ExecutionStatus.REJECT
    assert broker.trade_state("trade-NG-a").confirmed_stop == Decimal("99")
    assert moved.status is ExecutionStatus.ACK


def test_addressed_entry_has_no_retroactive_target_and_bar_replay_is_idempotent(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan())
    broker.submit(OpenTrade("open", "trade-NG-a", 0, "entry", 2), NOW)

    _bar(broker, NOW, 100, 99, 105, 104)
    assert broker.manager.positions == {}

    entry_bar = NOW + timedelta(minutes=1)
    _bar(broker, entry_bar, 100, 99, 105, 104)
    trade = broker.trade_state("trade-NG-a")
    assert broker.manager.positions["trade-NG-a"].qty == 2
    assert trade.target_filled == {"target-1": 0, "target-2": 0}

    _bar(broker, entry_bar, 100, 99, 105, 104)
    assert broker.manager.positions["trade-NG-a"].qty == 2
    assert trade.target_filled == {"target-1": 0, "target-2": 0}

    _bar(broker, NOW + timedelta(minutes=2), 101, 100, 102, 101)
    assert broker.manager.positions["trade-NG-a"].qty == 1
    assert trade.target_filled["target-1"] == 1


def test_addressed_stop_wins_over_target_and_gap_uses_open_before_signal_exit(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan())
    broker.submit(OpenTrade("open", "trade-NG-a", 0, "entry", 2), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100, 99, 101, 100)
    broker.submit(CloseTrade("close", "trade-NG-a", 1, "signal"), NOW + timedelta(minutes=1))

    _bar(broker, NOW + timedelta(minutes=2), 95, 94, 105, 100)

    trade = broker.trade_state("trade-NG-a")
    assert broker.manager.positions == {}
    assert trade.last_stop_fill == Decimal("95")
    assert trade.target_filled == {"target-1": 0, "target-2": 0}


def test_addressed_stop_amendment_only_applies_on_next_bar(tmp_path):
    broker = JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )
    broker.register_trade(_plan())
    broker.submit(OpenTrade("open", "trade-NG-a", 0, "entry", 2), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100, 99, 101, 100)
    broker.submit(MoveStop("move", "trade-NG-a", 1, "trail", Decimal("99")), NOW + timedelta(minutes=1))

    _bar(broker, NOW + timedelta(minutes=2), 100, 98.5, 101, 100)
    assert broker.manager.positions == {}
    assert broker.trade_state("trade-NG-a").last_stop_fill == Decimal("99")
