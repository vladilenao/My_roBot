from datetime import datetime, timezone

import pandas as pd

from src.broker import JournalBroker
from src.broker.exec_adapter import BrokerExecutionAdapter
from src.portfolio import ContractMeta, OrderStatus, PositionManager
from src.strategies.contracts import Decision, SignalType
from src.trade_journal import TradeJournal

UTC = timezone.utc

NG = ContractMeta(ticker="NG", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0)
NOW = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)
T1 = pd.Timestamp("2026-09-14 10:00:00")
T2 = pd.Timestamp("2026-09-14 10:05:00")


class FakeInstrument:
    ticker = "NG"


def _entry(bar_time, price=100.0):
    return Decision(
        signal_type=SignalType.BUY,
        price=price,
        stop_loss=price - 2.0,
        timeframe="5m",
        strategy_name="ma_cloud_rsi_macd",
        indicator_values={},
        bar_time=bar_time,
        risk_pct=2.0,
        risk_rub=2000.0,
        action="entry",
    )


def _scale_in(bar_time):
    return Decision(
        signal_type=SignalType.BUY,
        price=100.5,
        stop_loss=98.0,
        timeframe="5m",
        strategy_name="ma_cloud_rsi_macd",
        indicator_values={},
        bar_time=bar_time,
        risk_pct=2.0,
        risk_rub=2000.0,
        action="scale_in",
    )


def _exit(bar_time):
    return Decision(
        signal_type=SignalType.SELL,
        price=99.0,
        timeframe="5m",
        strategy_name="ma_cloud_rsi_macd",
        indicator_values={},
        bar_time=bar_time,
        action=None,
        exit_reason="close_below_ma40",
    )


def _adapter(tmp_path):
    journal = TradeJournal.created_on_init(tmp_path / "j.csv")
    manager = PositionManager(None, initial_deposit=100000, max_risk_pct=2.0)
    broker = JournalBroker(journal, manager, ["14:05", "19:00"])
    adapter = BrokerExecutionAdapter(broker, manager)
    adapter.set_contracts({"NG": NG})
    return adapter, broker, journal, manager


class TestBrokerEntryDedup:
    def test_one_entry_per_bar_second_is_duplicate(self, tmp_path):
        """Два одинаковых входящих сигнала за один бар → один ордер, второй `duplicate`."""
        adapter, broker, journal, manager = _adapter(tmp_path)
        first = adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        assert first.status is OrderStatus.NEW
        assert first.qty > 0

        second = adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        assert second.status is OrderStatus.CANCELLED
        assert second.reason == "duplicate"

        assert len(manager.pending) == 1
        reasons = [e.reason for e in journal.events()]
        assert reasons.count("duplicate") == 1
        notes = " ".join(e.notes for e in journal.events())
        assert "ma_cloud_rsi_macd" in notes

    def test_next_bar_creates_new_entry(self, tmp_path):
        """Разные бары — новые ордера: карта помнит только последний бар."""
        adapter, broker, journal, manager = _adapter(tmp_path)
        first = adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        assert first.status is OrderStatus.NEW
        second = adapter.execute(_entry(T2), FakeInstrument(), timeframe="5m")
        assert second.status is OrderStatus.NEW
        assert second.reason != "duplicate"
        assert len(manager.pending) == 2
        assert all(e.reason != "duplicate" for e in journal.events())

    def test_scale_in_not_blocked_by_dedup(self, tmp_path):
        """Добор с тем же bar_time, что и вход, не считается дубликатом."""
        adapter, broker, journal, manager = _adapter(tmp_path)
        first = adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        assert first.status is OrderStatus.NEW

        scaled = adapter.execute(_scale_in(T1), FakeInstrument(), timeframe="5m")
        assert scaled.status is OrderStatus.NEW
        assert scaled.reason != "duplicate"
        assert len(manager.pending) == 2

    def test_exit_not_blocked_by_dedup(self, tmp_path):
        """Выход с тем же bar_time, что и вход, не проходит через дедупликацию."""
        adapter, broker, journal, manager = _adapter(tmp_path)
        adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        broker.track_bar(NOW, {"NG": (99.0, 101.0, 100.0)}, {"NG": NG})
        assert len(manager.positions) == 1

        result = adapter.execute(_exit(T1), FakeInstrument(), timeframe="5m")
        assert result.status is OrderStatus.FILLED
        assert result.reason == "signal"
        assert manager.positions == {}

    def test_no_bar_time_does_not_dedup(self, tmp_path):
        """Если bar_time не задан (защитная ветка), дедупликация не вмешивается."""
        adapter, broker, journal, manager = _adapter(tmp_path)
        first = adapter.execute(_entry(None), FakeInstrument(), timeframe="5m")
        assert first.status is OrderStatus.NEW
        second = adapter.execute(_entry(None), FakeInstrument(), timeframe="5m")
        assert second.status is OrderStatus.NEW
        assert len(manager.pending) == 2

    def test_different_instruments_do_not_collide(self, tmp_path):
        """Ключ дедупликации включает инструмент: входы разных тикеров не мешают."""
        adapter, broker, journal, manager = _adapter(tmp_path)

        class OtherInstrument:
            ticker = "BR"

        adapter.set_contracts(
            {"BR": ContractMeta(ticker="BR", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0)}
        )
        first = adapter.execute(_entry(T1), FakeInstrument(), timeframe="5m")
        assert first.status is OrderStatus.NEW
        other = adapter.execute(_entry(T1), OtherInstrument(), timeframe="5m")
        assert other.status is OrderStatus.NEW
        assert len(manager.pending) == 2