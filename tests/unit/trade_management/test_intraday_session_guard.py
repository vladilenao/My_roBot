"""Сессионная защита внутридневных сделок в TradeManager (add-intraday-session-guard).

Настоящие ``Storage`` и ``TradeManager``; расписание — ответ брокера за 15–19.10.2026,
брокер — подставной. Время в тестах — московское (``msk``), внутри — naive UTC.
"""

import csv
import json
from datetime import timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.export import CsvExporter
from src.trade_journal.storage import Storage
from src.trade_management.actions import AddToTrade, CancelEntry, CloseTrade, MoveStop, OpenTrade, ReduceTrade
from src.trade_management.intraday_settings import IntradaySettings
from src.trade_management.manager import SignalAdmission, TradeManager
from src.trade_management.models import (
    CURRENT_ALGORITHM, CostSnapshot, EntryOrderType, ProfileSnapshot, TargetPlan, TradePlan,
)
from tests.support.session_schedule import FUTURES, msk
from tests.unit.trade_management.test_signal_pipeline import INSTRUMENT, LIMITS, NG_META, PROFILES

PLANNED = SignalAdmission()


class Broker:
    """Вход по рынку исполняется сразу (или только подтверждается при ``ack_only``)."""

    def __init__(self, *, ack_only=False):
        self.ack_only = ack_only
        self.actions = []
        self.open = {}

    def register_trade(self, plan):
        return None

    def contract_for(self, ticker):
        return NG_META

    def submit(self, action, now):
        self.actions.append(action)
        if isinstance(action, CancelEntry):
            status, quantity = ExecutionStatus.CANCEL, 0
        elif self.ack_only and isinstance(action, OpenTrade):
            status, quantity = ExecutionStatus.ACK, 0
        elif isinstance(action, MoveStop):
            status, quantity = ExecutionStatus.ACK, 0
        elif isinstance(action, CloseTrade):
            status, quantity = ExecutionStatus.FILL, self.open.pop(action.trade_id, 0)
        else:
            status, quantity = ExecutionStatus.FILL, action.quantity
            delta = quantity if isinstance(action, (OpenTrade, AddToTrade)) else -quantity
            self.open[action.trade_id] = self.open.get(action.trade_id, 0) + delta
        return ExecutionEvent(f"{action.command_id}:{status.value}", action.command_id, action.command_id,
                              action.trade_id, status, quantity, Decimal(100), Decimal(0), now, action.reason)


class Env:
    def __init__(self, storage, now, *, calendar=FUTURES, settings=None, ack_only=False):
        self.storage = storage
        self.now = now
        self.calendar = calendar
        self.broker = Broker(ack_only=ack_only)
        self.manager = TradeManager(
            storage, self.broker, initial_balance=Decimal(100000), profiles_config=PROFILES,
            risk_limits=LIMITS, max_qty=4, clock=lambda: self.now,
            intraday_settings=settings or IntradaySettings(),
            session_calendar=lambda instrument, now: self.calendar,
        )
        self.planned = []
        self._real_plan_entry = self.manager._plan_entry
        self.manager._plan_entry = self._plan_entry

    def _plan_entry(self, *args, **kwargs):
        self.planned.append(args)
        return PLANNED

    def signal(self, timeframe="5m", assignment_id="future-br-macd", event_id="signal"):
        assignment = SimpleNamespace(id=assignment_id, strategy="macd_rsi_stoch", management="levels_rr",
                                     filter_profile="raw", priority=0, timeframe=timeframe, clusters="off")
        decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=self.now, event_id=event_id,
                            available_at=self.now, timeframe=timeframe)
        return self.manager.actions_for_signal(assignment, decision, INSTRUMENT, _frame(100.0, self.now), None,
                                               timeframe=timeframe)

    def open(self, timeframe="5m", *, trade_id="trade-1", v2=False, quantity=4):
        plan = _plan(timeframe, trade_id=trade_id, v2=v2, created_at=self.now)
        action = (OpenTrade(f"open-{trade_id}", trade_id, 0, "entry", quantity, order_type="limit",
                            limit_price=Decimal(100)) if v2 else
                  OpenTrade(f"open-{trade_id}", trade_id, 0, "entry", quantity))
        assert self.manager.submit_plan(plan, action, price_step=Decimal(1), step_cost=Decimal(1))
        self.manager.dispatch(self.now.replace(tzinfo=timezone.utc))
        return plan

    def manage(self, close=100.0, timeframe="1m"):
        actions = self.manager.manage(INSTRUMENT, [], _frame(close, self.now), None, timeframe=timeframe,
                                      now=self.now.replace(tzinfo=timezone.utc))
        self.manager.dispatch(self.now.replace(tzinfo=timezone.utc))
        return actions

    def phase(self, trade_id="trade-1"):
        return self.storage.connection.execute("SELECT phase FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0]

    def traces(self, reason=None):
        sql, args = "SELECT reason, input_json FROM calculations WHERE algorithm='session.intraday'", []
        if reason is not None:
            sql, args = sql + " AND reason=?", [reason]
        return self.storage.connection.execute(sql, args).fetchall()


def _frame(close, now):
    end = pd.Timestamp(now).tz_localize("UTC") - pd.Timedelta(minutes=1)
    index = pd.date_range(end=end, periods=3, freq="min")
    return pd.DataFrame({"datetime": index, "open": close, "high": close + 1, "low": close - 1, "close": close})


def _plan(timeframe, *, trade_id="trade-1", v2=False, created_at=None):
    targets = (TargetPlan("tp-1", Decimal(104), Decimal("0.5")), TargetPlan("tp-2", Decimal(108), Decimal("0.5")))
    extra = dict(algorithm_version=CURRENT_ALGORITHM, cost_snapshot=CostSnapshot(Decimal("1.5"), Decimal(1)),
                 entry_order_type=EntryOrderType.LIMIT) if v2 else {}
    return TradePlan(trade_id, "future-br-macd", "NGV6", "BUY", f"signal-{trade_id}", Decimal(100), Decimal(96),
                     targets, ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}),
                     (created_at or msk(16, 10)).replace(tzinfo=timezone.utc), timeframe=timeframe, **extra)


def _take_tp1(env, *, stop="96", trade_id="trade-1"):
    """Состояние после TP1: 2 из 4 закрыты по 104, G=8 ₽, F=9 ₽, стоп подтверждён."""
    storage = env.storage
    env.broker.open[trade_id] = 2
    storage.connection.execute("UPDATE targets SET status='FILLED', filled_quantity=2 WHERE target_id='tp-1'")
    storage.connection.execute("UPDATE positions SET quantity=2, realized_pnl='8', fees='9' WHERE trade_id=?", (trade_id,))
    storage.connection.execute("UPDATE protection SET confirmed_stop=? WHERE trade_id=?", (stop, trade_id))
    storage.connection.execute("UPDATE trades SET phase='REDUCING' WHERE trade_id=?", (trade_id,))
    storage.connection.commit()


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as opened:
        yield opened


# ── допуск входа ──

def test_evening_entry_like_br_15_10_is_rejected(storage):
    env = Env(storage, msk(15, 23, 10))
    admission = env.signal()
    assert [reason.code for reason in admission.rejections] == ["intraday-session-closed"]
    assert env.planned == []
    ((reason, inputs),) = env.traces()
    assert reason == "intraday-session-closed"
    assert json.loads(inputs)["plan_timeframe"]["value"] == "5m"


@pytest.mark.parametrize("moment,code", [
    (msk(16, 17, 55), None),
    (msk(16, 18), "intraday-close-near"),
])
def test_entry_cutoff_of_futures(storage, moment, code):
    env = Env(storage, moment)
    admission = env.signal()
    if code is None:
        assert admission is PLANNED
    else:
        assert [reason.code for reason in admission.rejections] == [code]
        assert "близко к концу" in admission.rejections[0].message


def test_swing_entry_is_not_limited(storage):
    env = Env(storage, msk(15, 23, 10))
    assert env.signal(timeframe="1h") is PLANNED


def test_signal_of_owned_trade_goes_to_management(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 20)
    admission = env.signal(assignment_id="future-br-macd", event_id="signal-2")
    assert not admission.rejections


def test_disabled_rule_admits_evening_entry(storage):
    env = Env(storage, msk(15, 23, 10), settings=IntradaySettings(enabled=False))
    assert env.signal() is PLANNED


def test_unknown_schedule_rejects_intraday_entry(storage):
    env = Env(storage, msk(16, 12), calendar=None)
    assert [reason.code for reason in env.signal().rejections] == ["session-schedule-unknown"]
    assert env.signal(timeframe="1h", event_id="swing") is PLANNED


def test_calendar_failure_is_unknown_schedule(storage):
    env = Env(storage, msk(16, 12))

    def broken(instrument, now):
        raise RuntimeError("network")

    env.manager._session_calendar = broken
    assert [reason.code for reason in env.signal().rejections] == ["session-schedule-unknown"]


# ── снятие ожидающего входа ──

def test_pending_entry_is_cancelled_at_entry_cutoff(storage):
    env = Env(storage, msk(16, 17, 50), ack_only=True)
    env.open()
    assert env.phase() == "ENTRY_PENDING"
    env.now = msk(16, 17, 59)
    assert env.manage() == ()
    env.now = msk(16, 18)
    (cancel,) = env.manage()
    assert isinstance(cancel, CancelEntry) and cancel.reason == "intraday-session-end"
    assert env.phase() == "CANCELLED"
    assert env.manage() == ()


def test_pending_swing_entry_is_not_cancelled(storage):
    env = Env(storage, msk(16, 17, 50), ack_only=True)
    env.open(timeframe="1h")
    env.now = msk(16, 18)
    assert env.manage() == ()
    assert env.phase() == "ENTRY_PENDING"


def test_single_cancel_when_timeout_is_also_due(storage):
    env = Env(storage, msk(16, 16, 50), ack_only=True)
    env.open()
    env.now = msk(16, 18, 5)
    (cancel,) = env.manage()
    assert cancel.reason == "entry-timeout"
    assert sum(isinstance(action, CancelEntry) for action in env.broker.actions) == 1


# ── закрытие перед концом блока ──

def test_position_is_closed_at_flatten_moment_once(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 18, 39)
    assert env.manage() == ()
    env.now = msk(16, 18, 40)
    (close,) = env.manage()
    assert isinstance(close, CloseTrade) and close.reason == "intraday-session-end"
    assert env.phase() == "CLOSED"
    assert env.manage() == ()
    ((reason, inputs),) = env.traces("intraday-session-end")
    assert json.loads(inputs)["flatten_at"]["value"] == msk(16, 18, 40).isoformat()


def test_pending_close_is_not_duplicated(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 18, 40)
    env.manager.manage(INSTRUMENT, [], _frame(100.0, env.now), None, timeframe="1m",
                       now=env.now.replace(tzinfo=timezone.utc))
    assert env.manager.manage(INSTRUMENT, [], _frame(100.0, env.now), None, timeframe="1m",
                              now=env.now.replace(tzinfo=timezone.utc)) == ()


def test_position_is_closed_after_restart_in_evening(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 20, 15)
    (close,) = env.manage()
    assert isinstance(close, CloseTrade)


def test_swing_position_stays(storage):
    env = Env(storage, msk(16, 12))
    env.open(timeframe="1h")
    env.now = msk(16, 18, 40)
    assert env.manage() == ()


def test_decisions_only_on_protection_timeframe(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 18, 45)
    assert env.manage(timeframe="5m") == ()
    assert env.phase() == "OPEN"


def test_unknown_schedule_does_not_close(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.calendar = None
    env.now = msk(16, 18, 45)
    assert env.manage() == ()


# ── перенос защищённой позиции ──

def test_tp1_position_moves_stop_to_break_even_and_is_carried(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env)
    env.now = msk(16, 18, 40)
    (move,) = env.manage(close=105.0)
    # BE = 100 + (F 9 + выход 2×2 − G 8)/2 = 102.5 → вверх до шага 1 = 103.
    assert isinstance(move, MoveStop) and move.reason == "intraday-carry-break-even"
    assert move.stop_price == Decimal(103)
    assert env.phase() == "REDUCING"


def test_market_at_break_even_is_not_enough(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env)
    env.now = msk(16, 18, 40)
    (close,) = env.manage(close=103.0)
    assert isinstance(close, CloseTrade)


def test_unconfirmed_break_even_waits_then_closes_below_it(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env)

    def manage(moment, close):
        env.now = moment
        return env.manager.manage(INSTRUMENT, [], _frame(close, moment), None, timeframe="1m",
                                  now=moment.replace(tzinfo=timezone.utc))

    (move,) = manage(msk(16, 18, 40), 105.0)
    assert isinstance(move, MoveStop)
    assert manage(msk(16, 18, 41), 105.0) == ()
    (close,) = manage(msk(16, 18, 42), 101.0)
    assert isinstance(close, CloseTrade)


def test_stop_at_break_even_is_carried_and_rechecked_next_day(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env, stop="104")
    env.now = msk(16, 18, 40)
    assert env.manage(close=105.0) == ()
    state = json.loads(storage.connection.execute("SELECT profile_state_json FROM trades").fetchone()[0])
    assert state["intraday_carry_at"] == msk(16, 18, 40).isoformat()
    for moment in (msk(16, 20), msk(17, 12), msk(19, 8)):
        env.now = moment
        assert env.manage(close=105.0) == ()
    assert len(env.traces("intraday-carry")) == 1
    env.now = msk(19, 18, 40)
    assert env.manage(close=105.0) == ()
    assert len(env.traces("intraday-carry")) == 2


def test_tp1_with_market_below_break_even_closes(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env)
    env.now = msk(16, 18, 40)
    (close,) = env.manage(close=101.0)
    assert isinstance(close, CloseTrade)


def test_without_first_target_position_closes(storage):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    env.now = msk(16, 18, 40)
    (close,) = env.manage(close=103.0)
    assert isinstance(close, CloseTrade)


# ── доборы и выходы вне окна ──

def test_add_outside_entry_window_is_refused(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 20)
    assert not env.signal(event_id="signal-2").rejections   # сигнал своей связки регистрирует инструмент
    add = AddToTrade("add-1", "trade-1", 1, "levels-profitable-add", 1)
    assert env.manager.submit_action(add, market_close=Decimal(100)) is False
    assert [reason for reason, _ in env.traces()] == ["intraday-session-closed"]


def test_exit_in_evening_is_not_limited(storage):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 20)
    revision = storage.connection.execute("SELECT state_revision FROM trades").fetchone()[0]
    reduce = ReduceTrade("tp2", "trade-1", revision, "tp", 2, target_id="tp-2")
    assert env.manager.submit_action(reduce, market_close=Decimal(108)) is True


# ── журнал: русские причины ──

def _csv(storage, tmp_path):
    CsvExporter(storage.connection, tmp_path / "trade_event.csv", tmp_path / "trade_summary.csv").export()

    def read(name):
        with open(tmp_path / name, encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))

    return read("trade_event.csv"), read("trade_summary.csv")


def test_session_close_reason_in_csv(storage, tmp_path):
    env = Env(storage, msk(16, 12))
    env.open()
    env.now = msk(16, 18, 40)
    env.manage()
    events, summary = _csv(storage, tmp_path)
    assert "закрытие перед концом торговой сессии" in {row["Причина"] for row in events}
    assert summary[0]["Финальная причина"] == "закрытие перед концом торговой сессии"


def test_cancelled_entry_reason_in_csv(storage, tmp_path):
    env = Env(storage, msk(16, 17, 50), ack_only=True)
    env.open()
    env.now = msk(16, 18)
    env.manage()
    events, _ = _csv(storage, tmp_path)
    (cancel,) = [row for row in events if row["Событие"] == "Заявка отменена"]
    assert cancel["Причина"] == "закрытие перед концом торговой сессии"


def test_carry_stop_reason_in_csv(storage, tmp_path):
    env = Env(storage, msk(16, 12))
    env.open(v2=True)
    _take_tp1(env)
    env.now = msk(16, 18, 40)
    env.manage(close=105.0)
    events, _ = _csv(storage, tmp_path)
    assert "безубыток для переноса через перерыв: 96 → 103" in {row["Причина"] for row in events}
