"""``TInvestBroker`` с настоящими ``Storage``, reducer и ``TradeManager`` (add-tinvest-orders).

Инцидент 02.11 (SiZ6, 10 лотов): события брокера несли номер заявки брокера в ``order_id``, а
reducer сверяет его с заявкой журнала сделок — ACK и исполнение входа падали с
``execution order does not match command``. Плюс исполнение по сумме заявки и сделка стрима
давали 20 лотов на заявку в 10.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from src.broker.port import ExecutionStatus
from src.broker_contour.api import BrokerTrade
from src.broker_contour.broker import ExitPolicy, TInvestBroker
from src.broker_contour.orders import OrderLedger, order_key
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_management.actions import EntryOrderType, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CURRENT_ALGORITHM, CostSnapshot, ProfileSnapshot, TargetPlan, TradePlan
from tests.unit.broker_contour.fake_api import NG, NG_META, NOW, Clock, FakeApi

D = Decimal
TRADE = "future-ng-macd:NG:5m:2026-10-30T10:00:00:BUY"
RUB_PER_POINT = D("8086.64")


def _plan() -> TradePlan:
    return TradePlan(
        TRADE, "future-ng-macd", NG, "BUY", "signal-1", D("2.845"), D("2.812"),
        (TargetPlan("tp-1", D("2.911"), D("0.5"), D("0.066")), TargetPlan("tp-2", D("2.977"), D("0.5"), D("0.132"))),
        ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="5m", algorithm_version=CURRENT_ALGORITHM,
        cost_snapshot=CostSnapshot(D("0"), D("0")), entry_order_type=EntryOrderType.LIMIT, price_step=D("0.001"))


class Env:
    def __init__(self, tmp_path, storage):
        self.api, self.clock, self.storage = FakeApi(), Clock(), storage
        self.ledger = OrderLedger(tmp_path / "broker.sqlite3")
        self.broker = TInvestBroker(self.api, self.ledger, contour="sandbox", policy=ExitPolicy(5, 15, 2, 60),
                                    journal_has=self._journal_has, clock=self.clock)
        self.broker.set_contracts({NG: NG_META})
        self.manager = TradeManager(storage, self.broker, initial_balance=D(1_000_000), clock=self.clock)
        self.command = f"{TRADE}:entry"

    def _journal_has(self, execution_id: str) -> bool:
        return self.storage.connection.execute(
            "SELECT 1 FROM fills WHERE execution_id=? UNION SELECT 1 FROM events WHERE event_id=?",
            (execution_id, execution_id)).fetchone() is not None

    def enter(self, lots: int) -> str:
        action = OpenTrade(self.command, TRADE, 0, "signal", lots, EntryOrderType.LIMIT, D("2.8457"))
        assert self.manager.submit_plan(_plan(), action)
        self.storage.reserve_candidates((ReservationCandidate(
            reservation_id=f"reservation-{TRADE}", trade_id=TRADE, order_id=self.command, priority=0,
            assignment_id="future-ng-macd", instrument_id=NG, signal_id="signal-1",
            risk_amount=D(1000), margin_amount=D(0)),), risk_budget=D(100_000), margin_budget=D(100_000))
        self.manager.dispatch(self.clock())                  # ACK брокера проходит reducer
        return order_key(self.command)

    def filled_by_sum(self, key: str, lots: int, price: str) -> None:
        self.api.orders[key] = replace(self.api.orders[key], status="fill", lots_executed=lots, trades=(),
                                       executed_rub=D(price) * lots * RUB_PER_POINT,
                                       initial_rub=D("2.845") * lots * RUB_PER_POINT)

    def stream_fill(self, key: str, lots: int, price: str) -> None:
        self.broker.ingest(replace(self.api.orders[key], status="fill", lots_executed=lots, executed_rub=None,
                                   trades=(BrokerTrade("exch-stream-1", D(price), lots, NOW),)))

    def journal(self):
        order = self.storage.connection.execute(
            "SELECT order_id, status, quantity, filled_quantity FROM orders WHERE command_id=?",
            (self.command,)).fetchone()
        fills = self.storage.connection.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE trade_id=?",
                                                (TRADE,)).fetchone()[0]
        position = self.storage.connection.execute("SELECT quantity FROM positions WHERE trade_id=?",
                                                   (TRADE,)).fetchone()[0]
        return order, fills, position


@pytest.mark.parametrize("order", ["state-then-stream", "stream-then-state"])
def test_entry_fill_reaches_journal_once(tmp_path, order):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(tmp_path, storage)
        key = env.enter(10)
        assert storage.connection.execute("SELECT status FROM orders WHERE command_id=?",
                                          (env.command,)).fetchone()[0] == "ACK"
        if order == "state-then-stream":
            env.filled_by_sum(key, 10, "2.846")
            env.broker.service(env.clock())             # исполнение по сумме заявки — сделок стрима ещё нет
            env.manager.consume_queued_broker_events()
            env.stream_fill(key, 10, "2.846")
        else:
            env.stream_fill(key, 10, "2.846")
            env.filled_by_sum(key, 10, "2.846")
            env.broker.service(env.clock())
        env.manager.consume_queued_broker_events()
        env.manager.consume_queued_broker_events()

        (order_id, status, quantity, filled), fills, position = env.journal()
        assert (order_id, status, quantity, filled) == (env.command, "FILLED", 10, 10)
        assert fills == 10 and position == 10
        assert env.ledger.get(key).lots_executed == 10 and env.ledger.pending_executions() == []


def test_stuck_entry_recovers_after_restart(tmp_path):
    """Состояние тестового робота 02.11: ACK не записан, в журнале заявок 20 лотов — после рестарта 10."""
    from src.broker_contour.orders import LedgerExecution

    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(tmp_path, storage)
        key = env.enter(10)
        storage.connection.execute("UPDATE orders SET status='PENDING' WHERE command_id=?", (env.command,))
        storage.connection.execute("DELETE FROM events WHERE command_id=?", (env.command,))
        storage.connection.commit()
        broker_id = env.ledger.get(key).broker_order_id
        env.ledger.add_execution(LedgerExecution(f"tinvest-order:{broker_id}:10", key, 10, D("2.846"), NOW))
        env.ledger.add_execution(LedgerExecution("tinvest:exch-stream-1", key, 10, D("2.846"), NOW))
        env.ledger.update(key, state="filled", lots_executed=20, average=D("2.846"))
        env.ledger.close()

        restarted = Env(tmp_path, storage)              # новый процесс контура на тех же базах
        restarted.manager.restore()                     # как сборка контура: сделки регистрируются у брокера
        restarted.broker.recover(restarted.clock())
        restarted.manager.consume_queued_broker_events()

        (order_id, status, quantity, filled), fills, position = restarted.journal()
        assert (status, filled, fills, position) == ("FILLED", 10, 10, 10)
        assert restarted.ledger.get(key).lots_executed == 10
        assert [e.status for e in restarted.broker.drain_addressed_events()
                if e.status is ExecutionStatus.FILL] == []


def test_execution_refused_by_journal_is_not_hidden_in_flight(tmp_path):
    """Инцидент 02.11 (Si): лишнее исполнение выхода reducer отверг, оно навсегда осталось «в пути»,
    и сверка не видела расхождения со счётом. Теперь оно «отвергнуто», позиция брокера — по журналу."""
    from src.broker_contour.orders import REFUSED, LedgerExecution, LedgerOrder

    with Storage(tmp_path / "trades.sqlite3") as storage:
        env = Env(tmp_path, storage)
        key = env.enter(10)
        env.stream_fill(key, 10, "2.846")
        env.manager.consume_queued_broker_events()
        job = f"{TRADE}:pv:stop:2026-10-30T10:05:00+00:00"
        extra = order_key(f"{job}:1")
        env.ledger.create(LedgerOrder(extra, job, TRADE, "stop", NG, "SELL", "limit", 12, D("2.810"),
                                      state="filled", lots_executed=12, job=job))
        env.ledger.add_execution(LedgerExecution("tinvest:exch-extra", extra, 12, D("2.810"), NOW))

        env.manager.consume_queued_broker_events()          # reducer: сверх открытой позиции — не исключение

        _, fills, position = env.journal()
        assert (fills, position) == (10, 10)
        assert env.broker.trade_state(TRADE).position == 10
        assert [e.consumed for e in env.ledger.executions(extra)] == [REFUSED]
        assert env.ledger.pending_executions() == [] and env.broker.in_flight_instruments() == set()
        assert env.broker.drain_addressed_events() == []
