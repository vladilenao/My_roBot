"""Актуальность команд остановки контура при допуске доставки (fix-stop-command-freshness).

Настоящие ``Storage``, ``TradeManager`` и ``contour._dispatch_evidence``. Остановку
применяет ``control.before_tick()`` раньше, чем ``on_bar`` отмечает начало шага, поэтому
команда остановки всегда «создана раньше шага»: её актуальность доказывает действующий
режим остановки владельца, а не момент постановки в очередь.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.assembly import contour
from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.instruments import Instrument
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade
from src.trade_management.manager import CONTOUR_STOP, INSTRUMENT_REMOVE, STOP_COMMAND_RENEWED, TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

UID = "ed"
ITEM = Instrument("ED-12.26", "EDZ6", "future", "EDZ6", UID)
OPENED = datetime(2026, 10, 30, 14, 33, tzinfo=timezone.utc)
STOP_AT = datetime(2026, 10, 31, 12, 44, 41, tzinfo=timezone.utc)  # before_tick применил close_all
STEP_START = STOP_AT + timedelta(milliseconds=20)                  # on_bar того же тика


class FillBroker(BrokerPort):
    """Вход исполняется сразу, закрытие — всем остатком сделки."""

    def __init__(self):
        self.actions = []
        self.remaining = {}

    def register_trade(self, plan):
        pass

    def contract_for(self, ticker):
        return ContractMeta(ticker=ticker, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0,
                            expiration_date=None)

    def submit(self, action, now):
        self.actions.append(action)
        if isinstance(action, OpenTrade):
            quantity = self.remaining[action.trade_id] = action.quantity
        else:
            quantity = self.remaining.pop(action.trade_id)
        return ExecutionEvent(f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
                              ExecutionStatus.FILL, quantity, Decimal(100), Decimal(0), now, action.reason)


class Live:
    """Рыночные часы, начало шага и готовность пар live-контура."""

    def __init__(self, storage, broker=None, ready=True):
        self.now = OPENED
        self.step = [OPENED]
        self.state = "READY" if ready else "WAITING"
        self.broker = broker or FillBroker()
        self.storage = storage
        cache = SimpleNamespace(pair_status=lambda instrument, timeframe: {"state": self.state})
        self.manager = TradeManager(storage, self.broker, initial_balance=Decimal(100000), clock=lambda: self.now)
        self.manager.configure_dispatch_eligibility(
            contour._dispatch_evidence(cache, [ITEM], storage, self.step), nullcontext)

    def begin_step(self, start):
        self.now = start
        self.step[0] = start

    def dispatch(self):
        return self.manager.dispatch(self.now)


def open_position(live, trade="trade-1", quantity=5):
    plan = TradePlan(trade, "future-ed-macd", UID, "SELL", f"signal-{trade}", Decimal(100), Decimal(104),
                     (TargetPlan("tp", Decimal(96), Decimal(1)),), ProfileSnapshot("levels_rr", "1", {}),
                     OPENED, "1h")
    assert live.manager.submit_plan(plan, OpenTrade(f"open-{trade}", trade, 0, "entry", quantity))
    live.dispatch()


def closes(storage, trade="trade-1"):
    return storage.connection.execute(
        "SELECT o.command_id, o.status, b.status FROM orders o JOIN outbox b USING(command_id) "
        "WHERE o.trade_id=? AND o.action_type='CLOSE' ORDER BY o.created_at, o.rowid", (trade,)).fetchall()


def phase(storage, trade="trade-1"):
    return storage.connection.execute("SELECT phase FROM trades WHERE trade_id=?", (trade,)).fetchone()[0]


def stop_close_all(live, at=STOP_AT):
    """Тик контура: before_tick применяет остановку, затем on_bar отмечает начало шага."""
    live.now = at
    live.manager.stop_contour("close_all")
    live.begin_step(at + (STEP_START - STOP_AT))


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as opened:
        yield opened


def test_close_all_applied_before_step_start_is_delivered_in_its_tick(storage):
    live = Live(storage)
    open_position(live)

    stop_close_all(live)
    live.dispatch()

    assert phase(storage) == "CLOSED"
    assert [type(action) for action in live.broker.actions] == [OpenTrade, CloseTrade]
    assert live.manager.dispatch_block_reasons == {}


def test_close_all_held_by_data_is_delivered_on_a_later_step(storage):
    live = Live(storage)
    open_position(live)
    live.state = "WAITING"  # минуты инструмента не подтверждены

    stop_close_all(live)
    live.dispatch()
    command, = (row[0] for row in closes(storage))
    assert live.manager.dispatch_block_reasons == {command: "execution-prefix-unconfirmed"}

    # Через выходные пара готова; before_tick каждого тика повторяет остановку.
    live.now = STOP_AT + timedelta(days=2)
    assert live.manager.stop_contour("close_all") == ()
    live.begin_step(live.now + timedelta(milliseconds=20))
    live.state = "READY"
    live.dispatch()

    assert closes(storage) == [(command, "FILLED", "SENT")]
    assert phase(storage) == "CLOSED"


def test_close_all_left_in_journal_is_delivered_after_restart(tmp_path):
    path = tmp_path / "trades.sqlite3"
    broker = FillBroker()
    with Storage(path) as storage:
        live = Live(storage, broker)
        open_position(live)
        live.state = "WAITING"
        stop_close_all(live)
        live.dispatch()
        command, = (row[0] for row in closes(storage))

    with Storage(path) as storage:  # новый процесс: момент постановки неизвестен
        live = Live(storage, broker)
        stop_close_all(live, at=STOP_AT + timedelta(hours=3))
        live.dispatch()

        assert closes(storage) == [(command, "FILLED", "SENT")]
        assert phase(storage) == "CLOSED"


def test_lifted_stop_does_not_make_held_close_current(storage):
    live = Live(storage)
    open_position(live)
    live.state = "WAITING"
    stop_close_all(live)
    live.dispatch()

    live.manager.stop_contour(None)
    live.begin_step(STOP_AT + timedelta(minutes=5))
    live.state = "READY"
    live.dispatch()

    command, = (row[0] for row in closes(storage))
    assert live.manager.dispatch_block_reasons == {command: "freshness-unconfirmed"}
    assert phase(storage) == "OPEN"


def test_no_entries_does_not_make_held_close_current(storage):
    """Закрытие, созданное снятием инструмента close_all, не актуально по остановке контура без закрытий."""
    live = Live(storage)
    open_position(live)
    live.state = "WAITING"
    live.now = STOP_AT
    live.manager.stop_instrument(UID, "close_all")
    live.begin_step(STEP_START)
    live.dispatch()

    live.manager.stop_instrument(UID, None)
    live.manager.stop_contour("no_entries")
    live.begin_step(STOP_AT + timedelta(minutes=5))
    live.state = "READY"
    live.dispatch()

    command, payload = storage.connection.execute(
        "SELECT command_id, payload_json FROM outbox WHERE payload_json LIKE '%CloseTrade%'").fetchone()
    assert INSTRUMENT_REMOVE in payload
    assert live.manager.dispatch_block_reasons == {command: "freshness-unconfirmed"}


def test_instrument_close_all_is_current_while_instrument_is_removed(storage):
    live = Live(storage)
    open_position(live)
    live.now = STOP_AT
    live.manager.stop_instrument(UID, "close_all")
    live.begin_step(STEP_START)
    live.dispatch()

    assert phase(storage) == "CLOSED"


def test_held_close_with_stale_revision_is_renewed(storage):
    live = Live(storage)
    open_position(live)
    live.state = "WAITING"
    stop_close_all(live)
    live.dispatch()
    old, = (row[0] for row in closes(storage))
    # Пока закрытие удержано, ревизия сделки сменилась (например, исполнилась цель).
    storage.connection.execute("UPDATE trades SET state_revision = state_revision + 1 WHERE trade_id='trade-1'")
    storage.connection.commit()

    live.begin_step(STOP_AT + timedelta(days=2))
    live.state = "READY"
    renewed = live.manager.stop_contour("close_all")
    live.dispatch()

    assert [(type(action), action.reason) for action in renewed] == [(CloseTrade, CONTOUR_STOP)]
    rows = closes(storage)
    assert rows[0] == (old, "CANCEL", "UNDELIVERED")
    assert rows[1][1:] == ("FILLED", "SENT")
    assert phase(storage) == "CLOSED"
    assert storage.connection.execute(
        "SELECT json_extract(payload_json,'$.reason') FROM events WHERE command_id=?", (old,)).fetchone() == (
        STOP_COMMAND_RENEWED,)
    assert [type(action) for action in live.broker.actions] == [OpenTrade, CloseTrade]
