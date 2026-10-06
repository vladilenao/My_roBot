"""Причинное владение OHLC, экскурсии и однократность durable observations."""

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.broker.journal_broker import JournalBroker
from src.portfolio import ContractMeta, PositionManager
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CostSnapshot, PlanEconomics, ProfileSnapshot, TradePlan

D = Decimal
NOW = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
META = ContractMeta("SBER", 1, 1, 0, 0)


def system(tmp_path, side="BUY"):
    storage = Storage(tmp_path / "trades.sqlite3", journal_path=tmp_path / "events.csv", positions_path=tmp_path / "summary.csv")
    storage.set_names({"SBER": "SBER"})
    broker = JournalBroker(None, PositionManager(100000, 2), [], clock=lambda: NOW)
    broker.set_contracts({"SBER": META})
    manager = TradeManager(storage, broker, initial_balance=D(100000), clock=lambda: NOW)
    p = TradePlan("trade", "assignment", "SBER", side, "signal", D(100), D(96 if side == "BUY" else 104), (),
                  ProfileSnapshot("ma_cloud", "1", {}), NOW, algorithm_version="economics-v2",
                  entry_order_type="limit", cost_snapshot=CostSnapshot(D("1.5"), D(1)), price_step=D(1),
                  economics=PlanEconomics(10, D(40), None, D(40), None))
    manager.submit_plan(p, OpenTrade("entry", "trade", 0, "entry", 10, "limit", D(100)), price_step=D(1), step_cost=D(1))
    manager.dispatch(NOW)
    return storage, manager, broker


def bar(manager, broker, values, minute, *, gap=False):
    stamp = NOW+timedelta(minutes=minute)
    broker.track_bar(stamp, {"SBER": values}, {"SBER": META})
    for event in broker.drain_addressed_events():
        manager.consume(event)
    return manager.observe_bars({"SBER": values}, {"SBER": stamp}, gap_before=gap)


def card(tmp_path):
    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as stream:
        return next(csv.DictReader(stream))


def test_short_uses_held_bar_between_fills_and_excludes_high_after_gap_exit(tmp_path):
    with system(tmp_path, "SELL")[0] as storage:
        # Recreate the coordinator against the same durable state and restore ACK.
        broker = JournalBroker(None, PositionManager(100000, 2), [], clock=lambda: NOW)
        broker.set_contracts({"SBER": META})
        manager = TradeManager(storage, broker, initial_balance=D(100000), clock=lambda: NOW)
        manager.restore()
        bar(manager, broker, (100, 99, 102, 100), 1)
        bar(manager, broker, (100, 97, 103, 99), 2)  # held with no fill; MFE=30/40
        bar(manager, broker, (105, 90, 110, 100), 3)  # stop at open; only 105 belongs
        row = card(tmp_path)
        assert row["MAE (R)"] == "1.25" and row["MFE (R)"] == "0.75"
        assert row["Initial Risk (₽)"] == "40.00"
        assert row["Комиссия"] == "-30.00" and row["Net PnL"] == "-80.00"
        assert row["Полнота экстремумов"] == "полные доступные наблюдения"
        assert "Trade ID" not in row and row["Контракт"] == "SBER"
        final = storage.connection.execute("SELECT low,high,observed_price FROM trade_market_observations WHERE timeframe='1m' ORDER BY bar_id DESC LIMIT 1").fetchone()
        assert final == (None, None, "105")


def test_intrabar_entry_does_not_acquire_the_previous_high(tmp_path):
    storage, manager, broker = system(tmp_path)
    with storage:
        bar(manager, broker, (103, 99, 120, 101), 1)
        row = card(tmp_path)
        assert row["MAE (R)"] == "0.00" and row["MFE (R)"] == "0.00"
        assert row["Полнота экстремумов"] == "частичные"
        assert storage.connection.execute("SELECT low,high FROM trade_market_observations WHERE timeframe='1m'").fetchone() == (None, None)


def test_close_on_open_excludes_later_extreme_and_duration_is_only_closed(tmp_path):
    storage, manager, broker = system(tmp_path)
    with storage:
        bar(manager, broker, (100, 99, 102, 101), 1)
        assert card(tmp_path)["Длительность"] == ""
        state = storage.load_trade("trade").state
        manager.submit_action(CloseTrade("close", "trade", state.state_revision, "profile-exit"), market_close=D(101))
        manager.dispatch(NOW+timedelta(minutes=2))
        bar(manager, broker, (101, 98, 150, 120), 2)
        assert card(tmp_path)["MFE (R)"] == "0.50"
        assert card(tmp_path)["Статус"] == "закрыта"
        assert manager.observe_bars({"SBER": (120, 100, 200, 130)}, {"SBER": NOW+timedelta(minutes=3)}) == 0


def test_gap_is_partial_without_interpolation_and_repeated_bar_is_idempotent(tmp_path):
    storage, manager, broker = system(tmp_path)
    with storage:
        assert bar(manager, broker, (100, 99, 102, 101), 1) == 1
        assert bar(manager, broker, (101, 100, 103, 102), 5, gap=True) == 1
        count = storage.connection.execute("SELECT COUNT(*) FROM trade_market_observations").fetchone()[0]
        assert bar(manager, broker, (101, 100, 103, 102), 5) == 0
        assert storage.connection.execute("SELECT COUNT(*) FROM trade_market_observations").fetchone()[0] == count
        assert card(tmp_path)["Полнота экстремумов"] == "частичные"
        assert card(tmp_path)["MFE (R)"] == "0.75"


def test_no_entry_means_unavailable_not_zero_excursions(tmp_path):
    storage, manager, broker = system(tmp_path)
    with storage:
        bar(manager, broker, (102, 101, 103, 102), 1)
        row = card(tmp_path)
        assert row["MAE (R)"] == "" and row["MFE (R)"] == ""
        assert row["Полнота экстремумов"] == "нет наблюдений"


def test_favourable_only_held_bars_do_not_turn_into_adverse_excursion(tmp_path):
    storage, manager, broker = system(tmp_path)
    with storage:
        bar(manager, broker, (100, 100, 102, 101), 1)
        bar(manager, broker, (102, 102, 104, 103), 2)
        row = card(tmp_path)
        assert row["MAE (R)"] == "0.00" and row["MFE (R)"] == "1.00"
