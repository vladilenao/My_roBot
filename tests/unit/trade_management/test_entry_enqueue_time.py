"""Момент создания входа при проверке актуальности (fix-entry-command-enqueue-time).

Настоящие ``Storage``, ``TradeManager`` и ``contour._dispatch_evidence``. У входа
``outbox.created_at`` — время открытия свечи сигнала, а начало шага ``intent_since`` —
рыночные часы: до исправления любой живой вход считался созданным раньше шага.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.assembly import contour
from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.instruments import Instrument
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

UID = "u"
ITEM = Instrument("SBER", "SBER", "share", "SBER", UID)
CANDLE_OPEN = datetime(2026, 10, 15, 4, 45, tzinfo=timezone.utc)  # свеча 15m, по которой принято решение
STEP_START = datetime(2026, 10, 15, 5, 0, 1, tzinfo=timezone.utc)  # on_bar текущего шага
CREATED = datetime(2026, 10, 15, 5, 0, 2, tzinfo=timezone.utc)     # сигнал посчитан через секунду


class FillBroker(BrokerPort):
    def __init__(self):
        self.actions = []

    def register_trade(self, plan):
        pass

    def contract_for(self, ticker):
        return ContractMeta(ticker=ticker, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0,
                            expiration_date=None)

    def submit(self, action, now):
        self.actions.append(action)
        return ExecutionEvent(f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
                              ExecutionStatus.FILL, action.quantity, Decimal(100), Decimal(0), now, action.reason)


class Live:
    """Рыночные часы, начало шага и готовность данных live-пары в одном месте."""

    def __init__(self, storage, ready=True):
        self.now = CREATED
        self.step = [STEP_START]
        self.state = "READY" if ready else "WAITING"
        self.broker = FillBroker()
        self.storage = storage
        self.manager = self.new_manager()

    def new_manager(self):
        cache = SimpleNamespace(pair_status=lambda instrument, timeframe: {"state": self.state})
        manager = TradeManager(self.storage, self.broker, initial_balance=Decimal(100000), clock=lambda: self.now)
        manager.configure_dispatch_eligibility(
            contour._dispatch_evidence(cache, [ITEM], self.storage, self.step), nullcontext)
        return manager

    def begin_step(self, start):
        self.now = start
        self.step[0] = start


def entry_plan(trade="trade-1"):
    return TradePlan(trade, "binding-1", UID, "BUY", f"signal-{trade}", Decimal(100), Decimal(96),
                     (TargetPlan("tp", Decimal(104), Decimal(1)),), ProfileSnapshot("levels_rr", "1", {}),
                     CANDLE_OPEN, "15m")


def enter(live, trade="trade-1", command="open-1"):
    assert live.manager.submit_plan(entry_plan(trade), OpenTrade(command, trade, 0, "entry", 10))
    live.manager.dispatch(live.now)


def phase(storage, trade="trade-1"):
    return storage.connection.execute("SELECT phase FROM trades WHERE trade_id=?", (trade,)).fetchone()[0]


def outbox(storage, command="open-1"):
    return storage.connection.execute("SELECT status FROM outbox WHERE command_id=?", (command,)).fetchone()[0]


def test_entry_of_candle_opened_before_step_is_delivered_in_its_step(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        live = Live(storage)

        enter(live)

        assert outbox(storage) == "SENT"
        assert [action.command_id for action in live.broker.actions] == ["open-1"]
        assert live.manager.dispatch_block_reasons == {}


def test_delivered_entry_survives_next_step(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        live = Live(storage)
        enter(live)
        live.begin_step(CREATED + timedelta(minutes=1))

        assert live.manager.cancel_undelivered_increases(live.step[0]) == ()

        assert phase(storage) == "OPEN" and outbox(storage) == "SENT"


def test_entry_without_confirmed_data_is_cancelled_only_on_next_step(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        live = Live(storage, ready=False)
        enter(live)
        assert outbox(storage) == "PENDING"
        assert live.manager.dispatch_block_reasons == {"open-1": "execution-prefix-unconfirmed"}
        assert live.manager.cancel_undelivered_increases(STEP_START) == ()      # шаг создания: ещё не снят

        live.begin_step(CREATED + timedelta(minutes=1))
        events = live.manager.cancel_undelivered_increases(live.step[0])

        assert [(event.status, event.reason) for event in events] == [(ExecutionStatus.CANCEL, "entry-undelivered")]
        assert phase(storage) == "CANCELLED" and outbox(storage) == "UNDELIVERED"
        assert live.broker.actions == []


def test_held_entry_is_not_current_on_next_step_even_with_confirmed_data(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        live = Live(storage, ready=False)
        enter(live)
        live.begin_step(CREATED + timedelta(minutes=1))
        live.state = "READY"

        live.manager.dispatch(live.now)

        assert live.manager.dispatch_block_reasons == {"open-1": "freshness-unconfirmed"}
        assert live.broker.actions == []


def test_entry_left_in_journal_is_cancelled_on_first_step_after_restart(tmp_path):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        enter(Live(storage, ready=False))                                        # процесс остановлен с входом в очереди

    with Storage(path) as storage:                                               # новый процесс, пустая память
        live = Live(storage, ready=False)
        live.begin_step(CREATED + timedelta(seconds=30))

        events = live.manager.cancel_undelivered_increases(live.step[0])

        assert [event.reason for event in events] == ["entry-undelivered"]
        assert phase(storage) == "CANCELLED"


def test_outbox_created_at_keeps_candle_time(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        live = Live(storage)
        enter(live)

        stamp, = storage.connection.execute("SELECT created_at FROM outbox WHERE command_id='open-1'").fetchone()

        assert datetime.fromisoformat(stamp) == CANDLE_OPEN
