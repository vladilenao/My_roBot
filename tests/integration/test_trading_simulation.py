from datetime import UTC, datetime, timedelta

import pytest

from src.broker import JournalBroker
from src.broker.exec_adapter import BrokerExecutionAdapter
from src.portfolio import ContractMeta, OrderStatus, PositionManager
from src.strategies.contracts import Decision, SignalType
from src.trade_journal import TradeJournal

NG = ContractMeta(ticker="NG", price_step=1.0, step_cost=100.0, go_buy=100.0, go_sell=100.0)
NOW = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)


class FakeInstrument:
    ticker = "NG"


def _decision(price, stop_loss):
    return Decision(
        signal_type=SignalType.BUY,
        price=price,
        stop_loss=stop_loss,
        timeframe="1h",
        strategy_name="ma_cloud_rsi_macd",
        indicator_values={},
        bar_time=None,
        risk_pct=2.0,
        risk_rub=2000.0,
    )


def test_signal_to_clearing_to_restart(tmp_path):
    journal_path = tmp_path / "trade_journal.csv"
    journal = TradeJournal.created_on_init(journal_path)
    manager = PositionManager(None, initial_deposit=100000, max_risk_pct=2.0)
    broker = JournalBroker(journal, manager, ["14:05", "19:00"])
    adapter = BrokerExecutionAdapter(broker, manager)
    adapter.set_contracts({"NG": NG})

    # сигал → заявка
    result = adapter.execute(_decision(100.0, 98.0), FakeInstrument())
    assert result.status is OrderStatus.NEW and result.qty == 10
    assert len(manager.pending) == 1

    # исполнение входа
    broker.track_bar(NOW, {"NG": (99.0, 101.0, 100.0)}, {"NG": NG})
    assert len(manager.positions) == 1 and len(manager.pending) == 0

    # защитный стоп (бар пробивает 98)
    broker.track_bar(NOW + timedelta(minutes=2), {"NG": (97.0, 99.0, 98.0)}, {"NG": NG})
    assert manager.positions == {}

    # клиринг
    broker.run_clearing(NOW + timedelta(hours=5))
    events = journal.events()
    assert {e.status for e in events} == {"NEW", "FILLED", "CLEARING"}

    # рестарт восстанавливает баланс
    restored = TradeJournal.created_on_init(journal_path).replay(100000)
    assert restored.balance == pytest.approx(100000 + (98 - 100) / 1 * 100 * 10 - 2.0)
    assert restored.positions == {} and restored.orders == {}