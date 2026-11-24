"""Контракт порта брокера (refactor-broker-port, решение 8): ``LiveBroker`` на адаптере площадки.

Сценарии: вход и исполнение, цели, стоп у брокера и его срабатывание, потерянные ответы на заявку и
стоп-заявку, рестарт с досверкой, снятие входа. Ожидания — как у ``LiveBroker`` на Т-Инвест до переноса.
"""

from __future__ import annotations

from decimal import Decimal

from src.broker.port import ExecutionStatus
from src.broker_contour.broker import ExitPolicy, LiveBroker
from src.broker_contour.orders import OrderLedger, order_key
from src.broker_contour.stops import ACTIVE, EXECUTED, StopLedger
from src.trade_management.actions import CancelEntry, EntryOrderType, OpenTrade
from tests.unit.broker_contour.fake_api import NG, NG_META, Clock
from tests.unit.broker_contour.test_tinvest_broker import _plan

D = Decimal
TRADE = "trade-ng-1"
STOP = D("2.813")          # стоп плана 2.812 после пересчёта от средней 2.846


class Contour:
    """Брокер контура с журналом заявок и стоп-заявок на площадке; ``restart`` — новый процесс на тех же базах."""

    def __init__(self, venue, tmp_path):
        self.venue, self.tmp_path, self.clock = venue, tmp_path, Clock()
        self.journal: set[str] = set()
        self.broker = self._build()

    def _build(self) -> LiveBroker:
        ledger = OrderLedger(self.tmp_path / "broker.sqlite3")
        self.stops = StopLedger(ledger)
        broker = LiveBroker(self.venue.port, ledger, contour="sandbox", book=self.venue.book,
                            fills_prefix=self.venue.fills_prefix, margin_every=self.venue.margin_every,
                            policy=ExitPolicy(5, 15, 2, 60, stop_place_secs=30), journal_has=self.journal.__contains__,
                            clock=self.clock, stops=self.stops)
        broker.set_contracts({NG: NG_META})
        broker.set_names({NG: "NG-12.26"})
        return broker

    def restart(self) -> None:
        self.broker = self._build()
        self.broker.register_trade(_plan(TRADE))
        self.broker.recover(self.clock())

    def open(self, lots: int = 2):
        self.broker.register_trade(_plan(TRADE))
        action = OpenTrade(f"{TRADE}:entry", TRADE, 0, "signal", lots, EntryOrderType.LIMIT, D("2.8457"))
        return action, self.broker.submit(action, self.clock())

    def drain(self):
        events = self.broker.drain_addressed_events()
        self.journal.update(event.execution_id for event in events)
        return events

    def service(self, seconds: float = 0) -> None:
        self.broker.service(self.clock.advance(seconds))

    def filled(self, lots: int = 2) -> str:
        action, _ = self.open(lots)
        key = order_key(action.command_id)
        self.broker.ingest(self.venue.fill(key, lots, "2.846"))
        self.drain()
        return key


def test_entry_fill_becomes_execution_with_broker_prefix(venue, tmp_path):
    c = Contour(venue, tmp_path)
    action, answer = c.open()
    assert answer.status is ExecutionStatus.ACK and answer.reason == "accepted"
    assert [(call[3], call[4], call[5]) for call in venue.posts("limit")] == [("BUY", 2, D("2.845"))]
    c.broker.ingest(venue.fill(order_key(action.command_id), 2, "2.846"))
    events = c.drain()
    assert [(e.status, e.filled_quantity, e.price) for e in events] == [(ExecutionStatus.FILL, 2, D("2.846"))]
    assert events[0].execution_id.startswith(f"{venue.fills_prefix}:")
    assert c.broker.trade_state(TRADE).position == 2


def test_targets_and_broker_stop_after_entry(venue, tmp_path):
    c = Contour(venue, tmp_path)
    c.filled()
    c.service()
    targets = [call for call in venue.posts("limit") if call[3] == "SELL"]
    assert [(call[4], call[5]) for call in targets] == [(1, D("2.912")), (1, D("2.978"))]
    assert venue.active_stops() == [("SELL", 2, STOP)]
    c.service(5)
    assert venue.stop_posts() == 1                       # стоп-заявка не задваивается


def test_target_fill_is_target_execution(venue, tmp_path):
    c = Contour(venue, tmp_path)
    c.filled()
    c.service()
    target = next(call[2] for call in venue.posts("limit") if call[3] == "SELL")
    c.broker.ingest(venue.fill(target, 1, "2.912"))
    events = c.drain()
    assert [(e.reason, e.filled_quantity, e.price) for e in events] == [("tp:tp-1", 1, D("2.912"))]
    c.service()
    assert venue.active_stops() == [("SELL", 1, STOP)]


def test_broker_stop_trigger_closes_trade(venue, tmp_path):
    c = Contour(venue, tmp_path)
    c.filled()
    c.service()
    stop_id, report = venue.trigger("2.810")
    assert c.broker.ingest(report) is False             # нашего ключа у порождённой заявки нет
    c.service(1)
    events = c.drain()
    assert [(e.reason, e.filled_quantity, e.price) for e in events] == [("protective", 2, D("2.810"))]
    assert events[0].command_id.startswith(f"{TRADE}:pv:stop:{stop_id}:")
    assert c.broker.trade_state(TRADE).position == 0
    assert c.stops.rows(trade_id=TRADE)[-1].state == EXECUTED


def test_lost_entry_answer_found_after_restart(venue, tmp_path):
    c = Contour(venue, tmp_path)
    venue.lose_next_order_answer()
    action, answer = c.open()
    assert answer.status is ExecutionStatus.ACK and answer.reason == "broker-unknown"
    c.restart()
    assert len(venue.posts()) == 1                       # найдена у брокера, повтора нет
    assert c.broker.submit(action, c.clock()).status is ExecutionStatus.ACK and len(venue.posts()) == 1


def test_fills_during_downtime_settled_after_restart(venue, tmp_path):
    c = Contour(venue, tmp_path)
    action, _ = c.open()
    venue.fill(order_key(action.command_id), 2, "2.846")   # исполнена, пока процесса не было
    c.restart()
    events = c.drain()
    assert [(e.filled_quantity, e.price) for e in events] == [(2, D("2.846"))]


def test_lost_stop_answer_found_without_duplicate(venue, tmp_path):
    c = Contour(venue, tmp_path)
    c.filled()
    venue.lose_next_stop_answer()
    c.service()
    c.service(3)
    assert venue.active_stops() == [("SELL", 2, STOP)] and venue.stop_posts() == 1
    assert [row.state for row in c.stops.rows(trade_id=TRADE)] == [ACTIVE]


def test_cancel_entry(venue, tmp_path):
    c = Contour(venue, tmp_path)
    action, _ = c.open()
    key = order_key(action.command_id)
    cancel = CancelEntry(f"{TRADE}:cancel", TRADE, 1, "ttl")
    assert c.broker.submit(cancel, c.clock()).reason == "cancelling"
    assert venue.broker_order_id(key) in venue.cancelled_orders()
    c.broker.ingest(venue.cancelled(key))
    events = c.drain()
    assert [(e.status, e.command_id) for e in events][-1] == (ExecutionStatus.CANCEL, cancel.command_id)
