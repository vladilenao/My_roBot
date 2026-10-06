"""Экстремумы бара в событии исполнения.

Отклонение позиции внутри бара важнее цены, по которой сделка закрылась: стоп
исполняется на стопе, цель — на её цене, а高低 экстремумы бара остаются единственным
свидетельством того, насколько глубоко ходил рынок, пока позиция была открыта.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker import ExecutionStatus, JournalBroker
from src.portfolio import PositionManager
from src.trade_journal import TradeJournal
from src.trade_management.actions import CloseTrade, MoveStop, OpenTrade
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TICKER = "NG"


def _plan(target: Decimal = Decimal("110"), stop: Decimal = Decimal("96")) -> TradePlan:
    return TradePlan(
        "trade-NG", "assignment-1", TICKER, "BUY", "signal-1", Decimal("100"), stop,
        (TargetPlan("tp-1", target, Decimal("1"), target - Decimal("100")),),
        ProfileSnapshot("levels_rr", "1", {}), NOW,
    )


@pytest.fixture()
def broker(tmp_path):
    return JournalBroker(
        TradeJournal.created_on_init(tmp_path / "journal.csv"),
        PositionManager(initial_deposit=100_000, max_risk_pct=2),
        ["14:05"],
    )


def _bar(broker, now, open_, low, high, close):
    broker.track_bar(now, {TICKER: (open_, low, high, close)}, {})


def _drain(broker):
    return broker.drain_addressed_events()


def test_entry_fill_carries_the_range_of_its_bar(broker):
    """Вход на широком баре сохраняет весь диапазон, а не только цену входа."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 101.0, 98.5, 103.0, 102.0)

    event = next(event for event in _drain(broker) if event.status is ExecutionStatus.FILL)

    assert event.price == Decimal("101.0")
    assert event.market_low == Decimal("98.5")
    assert event.market_high == Decimal("103.0")


def test_extremes_differ_from_the_execution_price(broker):
    """Экстремумы бара не подменяются ценой исполнения."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100.0, 91.0, 100.0, 92.0)

    event = next(event for event in _drain(broker) if event.status is ExecutionStatus.FILL)

    assert event.market_low == Decimal("91.0")
    assert event.market_high == Decimal("100.0")
    assert event.market_low != event.price


def test_protective_stop_fill_carries_the_bar_that_stopped_it(broker):
    """Защитное закрытие по стопу сохраняет диапазон бара, на котором сработало."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100.0, 99.0, 100.0, 99.5)
    _drain(broker)
    _bar(broker, NOW + timedelta(minutes=2), 95.0, 90.0, 96.0, 91.0)

    stop_fill = next(event for event in _drain(broker) if ":pv:stop:" in event.command_id)

    assert stop_fill.price == Decimal("95.0")
    assert stop_fill.market_low == Decimal("90.0")
    assert stop_fill.market_high == Decimal("96.0")


def test_target_fill_carries_the_bar_that_reached_it(broker):
    """Исполнение цели сохраняет диапазон бара, на котором цель достигнута."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100.0, 99.0, 100.0, 99.5)
    _drain(broker)
    _bar(broker, NOW + timedelta(minutes=2), 100.0, 99.0, 112.0, 111.0)

    target_fill = next(event for event in _drain(broker) if ":pv:tp:" in event.command_id)

    assert target_fill.price == Decimal("110")
    assert target_fill.market_high == Decimal("112.0")


def test_event_outside_a_bar_leaves_the_extremes_empty(broker):
    """Вне бара поля пусты: цена исполнения не должна выдаваться за экстремум."""
    broker.register_trade(_plan())

    event = broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)

    assert event.market_low is None
    assert event.market_high is None


def test_out_of_bar_command_fill_leaves_the_extremes_empty(broker):
    """Немедленная команда без бара тоже не получает экстремумов."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100.0, 99.0, 100.0, 99.5)
    _drain(broker)
    broker._addressed_trades["trade-NG"].revision = 0
    event = broker.submit(MoveStop("ms-1", "trade-NG", 0, "tighten", Decimal("99")), NOW)

    assert event.market_low is None
    assert event.market_high is None


def test_signal_exit_on_a_bar_keeps_the_extremes(broker):
    """Закрытие по сигналу, исполнившееся на баре, сохраняет его диапазон."""
    broker.register_trade(_plan())
    broker.submit(OpenTrade("c1", "trade-NG", 0, "entry", 3), NOW)
    _bar(broker, NOW + timedelta(minutes=1), 100.0, 99.0, 100.0, 99.5)
    _drain(broker)
    state = broker.trade_state("trade-NG")
    broker.submit(CloseTrade("cc-1", "trade-NG", state.revision, "signal-exit"), NOW)
    _bar(broker, NOW + timedelta(minutes=2), 101.0, 100.5, 102.0, 101.5)

    close_fill = next(event for event in _drain(broker) if event.command_id == "cc-1")

    assert close_fill.market_low == Decimal("100.5")
    assert close_fill.market_high == Decimal("102.0")