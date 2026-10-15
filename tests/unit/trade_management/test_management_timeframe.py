"""Сопровождение сделки только на таймфрейме её плана (management-plan-timeframe)."""

import json
from decimal import Decimal
from types import SimpleNamespace

from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, MoveStop, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

from tests.unit.trade_management.test_signal_pipeline import (
    BAR0, BAR1, INSTRUMENT, LIMITS, NOW, PROFILES, _FillBroker, _frame,
)


RISING = [96.0 + i for i in range(45)]
DECLINING = [120.0 - i * (40.0 / 44.0) for i in range(45)]


def _ma_cloud_plan(timeframe="1h"):
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"),
        Decimal("92"), (),
        ProfileSnapshot("ma_cloud", "1", {"buffer_ticks": 1}), NOW, timeframe=timeframe,
    )


def _open(storage, plan):
    manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                           profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
    assert manager.submit_plan(plan, OpenTrade("open-1", plan.trade_id, 0, "entry", 2))
    manager.dispatch(BAR0)
    return manager


def _traces(storage, algorithm=None, reason=None):
    sql, args = "SELECT algorithm, reason, input_json FROM calculations WHERE 1=1", []
    if algorithm is not None:
        sql, args = sql + " AND algorithm=?", [*args, algorithm]
    if reason is not None:
        sql, args = sql + " AND reason=?", [*args, reason]
    return storage.connection.execute(sql, args).fetchall()


def _profile_state(storage):
    return storage.connection.execute("SELECT profile_state_json FROM trades WHERE trade_id='trade-1'").fetchone()[0]


def test_bar_of_other_timeframe_does_not_move_stop(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))
        state_before = _profile_state(storage)

        actions = manager.manage(INSTRUMENT, [], _frame(RISING), None, timeframe="15m")

        assert actions == ()
        assert _traces(storage, algorithm="profile.ma_cloud.manage") == []
        assert _profile_state(storage) == state_before


def test_bar_of_plan_timeframe_moves_stop(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))

        actions = manager.manage(INSTRUMENT, [], _frame(RISING), None, timeframe="1h")

        assert any(isinstance(action, MoveStop) for action in actions)


def test_signal_exit_only_on_plan_timeframe(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))

        assert manager.manage(INSTRUMENT, [], _frame(DECLINING), None, timeframe="15m") == ()
        assert manager.manage(INSTRUMENT, [], _frame(DECLINING), None, timeframe="1m") == ()
        actions = manager.manage(INSTRUMENT, [], _frame(DECLINING), None, timeframe="1h")

        assert any(isinstance(action, CloseTrade) for action in actions)


def test_manage_without_timeframe_does_not_run_profile(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))

        assert manager.manage(INSTRUMENT, [], _frame(DECLINING), None) == ()
        assert _traces(storage, algorithm="profile.ma_cloud.manage") == []


def test_unknown_plan_timeframe_is_not_managed_and_is_audited_once(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan(""))

        for timeframe in ("1m", "15m", "1h"):
            assert manager.manage(INSTRUMENT, [], _frame(DECLINING), None, timeframe=timeframe) == ()

        assert _traces(storage, algorithm="profile.ma_cloud.manage") == []
        assert len(_traces(storage, reason="management-timeframe-unknown")) == 1


def test_management_trace_records_bar_timeframe(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))

        manager.manage(INSTRUMENT, [], _frame(RISING), None, timeframe="1h")

        ((_, _, inputs),) = _traces(storage, algorithm="profile.ma_cloud.manage")
        assert json.loads(inputs)["timeframe"]["value"] == "1h"


def test_own_binding_signal_from_other_timeframe_is_ignored(tmp_path):
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"),
        Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW, timeframe="1h",
    )
    # Таймфрейм привязки в конфигурации сменён на 15m после открытия сделки на 1h.
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch", management="levels_rr",
                                 filter_profile="raw", priority=0, timeframe="15m")
    oppose = Decision(signal_type=SignalType.SELL, price=98.0, bar_time=BAR1,
                      event_id="signal-2", available_at=BAR1, timeframe="15m")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, plan)

        admission = manager.actions_for_signal(assignment, oppose, INSTRUMENT, _frame([98.0] * 25), None,
                                               timeframe="15m")

        assert admission.actions == ()
        assert len(_traces(storage, reason="management-timeframe-mismatch")) == 1
        phase = storage.connection.execute("SELECT phase FROM trades WHERE trade_id='trade-1'").fetchone()[0]
        assert phase == "OPEN"

        admission = manager.actions_for_signal(assignment, oppose, INSTRUMENT, _frame([98.0] * 25), None,
                                               timeframe="1h")

        assert any(isinstance(action, CloseTrade) for action in admission.actions)


def test_stop_raised_earlier_is_not_loosened_by_plan_timeframe_candidate(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _open(storage, _ma_cloud_plan("1h"))
        # Стоп уже подтянут выше кандидата 1h-облака (≈119 на растущем кадре).
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop='125' WHERE trade_id='trade-1'")

        actions = manager.manage(INSTRUMENT, [], _frame(RISING), None, timeframe="1h")

        assert not any(isinstance(action, MoveStop) for action in actions)
