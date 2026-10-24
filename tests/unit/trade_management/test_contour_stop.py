"""Остановка торгового контура командой в TradeManager (add-control-api).

Настоящие ``Storage`` и ``TradeManager``, подставной брокер из тестов сессионной
защиты. Таймфрейм 1h — вне сессионной защиты внутридневных сделок.
"""

from dataclasses import replace
from datetime import timezone

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.actions import AddToTrade, CancelEntry, CloseTrade, OpenTrade
from src.trade_management.manager import CONTOUR_STOP, CONTOUR_STOPPING
from tests.support.session_schedule import msk
from tests.unit.trade_management.test_intraday_session_guard import Env, _plan

TF = "1h"


def open_trade(env, trade_id, *, quantity=4):
    """Сделка своей привязки: одна незавершённая сделка на привязку и инструмент."""
    plan = replace(_plan(TF, trade_id=trade_id, created_at=env.now), assignment_id=f"future-br-{trade_id}")
    action = OpenTrade(f"open-{trade_id}", trade_id, 0, "entry", quantity)
    assert env.manager.submit_plan(plan, action, price_step=1, step_cost=1)
    env.manager.dispatch(env.now.replace(tzinfo=timezone.utc))


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as opened:
        yield opened


def test_signal_while_stopping_is_rejected_without_planning(storage):
    env = Env(storage, msk(16, 11))
    env.manager.stop_contour("no_entries")
    admission = env.signal(TF)
    assert [reason.code for reason in admission.rejections] == [CONTOUR_STOPPING]
    assert env.planned == []
    row = storage.connection.execute(
        "SELECT reason, outcome FROM calculations WHERE algorithm='contour.stop'").fetchone()
    assert row == (CONTOUR_STOPPING, "REJECTED")


def test_no_entries_cancels_pending_entry_and_keeps_open_position(storage):
    env = Env(storage, msk(16, 11))
    open_trade(env, "open")
    env.broker.ack_only = True
    open_trade(env, "pending")
    assert env.manager.contour_remainder() == (1, 1)

    actions = env.manager.stop_contour("no_entries")
    env.manager.dispatch(env.now.replace(tzinfo=timezone.utc))

    assert [(type(action), action.trade_id, action.reason) for action in actions] == [
        (CancelEntry, "pending", CONTOUR_STOP)]
    assert env.phase("pending") == "CANCELLED" and env.phase("open") == "OPEN"
    assert env.manager.contour_remainder() == (1, 0)


def test_close_all_closes_positions_once(storage):
    env = Env(storage, msk(16, 11))
    open_trade(env, "one")
    open_trade(env, "two")

    first = env.manager.stop_contour("close_all")
    # Повтор до исполнения: заявки уже в пути, второй раз не создаются.
    again = env.manager.stop_contour("close_all")
    env.manager.dispatch(env.now.replace(tzinfo=timezone.utc))

    assert sorted(action.trade_id for action in first if isinstance(action, CloseTrade)) == ["one", "two"]
    assert again == ()
    assert env.manager.contour_remainder() == (0, 0)
    assert sum(isinstance(action, CloseTrade) for action in env.broker.actions) == 2


def test_add_is_refused_while_stopping(storage):
    env = Env(storage, msk(16, 11))
    open_trade(env, "trade-1", quantity=2)
    env.manager.stop_contour("no_entries")
    revision = env.manager.restore()[0].state.state_revision
    add = AddToTrade("add-1", "trade-1", revision, "levels-profitable-add", 1)
    assert env.manager.submit_action(add, assignment_id="future-br-trade-1") is False
    assert not any(isinstance(action, AddToTrade) for action in env.broker.actions)


def test_stop_can_be_lifted_and_rejects_unknown_mode(storage):
    env = Env(storage, msk(16, 11))
    env.manager.stop_contour("no_entries")
    assert env.manager.stop_contour(None) == () and env.manager.contour_stop is None
    env.signal(TF)
    assert len(env.planned) == 1
    with pytest.raises(ValueError):
        env.manager.stop_contour("pause")
