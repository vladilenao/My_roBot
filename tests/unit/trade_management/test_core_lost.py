"""Запрет входов без связи с ядром в TradeManager (add-contour-recovery)."""

from datetime import timezone

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.actions import AddToTrade
from src.trade_management.manager import CONTOUR_STOPPING, CORE_LOST
from tests.support.session_schedule import msk
from tests.unit.trade_management.test_contour_stop import TF, open_trade
from tests.unit.trade_management.test_intraday_session_guard import Env


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as opened:
        yield opened


def _traces(storage):
    return storage.connection.execute(
        "SELECT reason, outcome FROM calculations WHERE algorithm='contour.core_link' ORDER BY rowid").fetchall()


def test_signal_is_rejected_and_hold_traced_once(storage):
    env = Env(storage, msk(16, 11))
    env.manager.hold_entries(True)
    env.manager.hold_entries(True)
    admission = env.signal(TF)
    assert [reason.code for reason in admission.rejections] == [CORE_LOST] and env.planned == []
    assert _traces(storage) == [(CORE_LOST, "REJECTED")]


def test_pending_entry_kept_and_add_refused(storage):
    env = Env(storage, msk(16, 11))
    open_trade(env, "open", quantity=2)
    env.broker.ack_only = True
    open_trade(env, "pending")
    env.manager.hold_entries(True)
    env.manager.dispatch(env.now.replace(tzinfo=timezone.utc))
    assert env.phase("pending") == "ENTRY_PENDING" and env.manager.contour_remainder() == (1, 1)
    revision = next(item.state.state_revision for item in env.manager.restore() if item.plan.trade_id == "open")
    add = AddToTrade("add-1", "open", revision, "levels-profitable-add", 1)
    assert env.manager.submit_action(add, assignment_id="future-br-open") is False


def test_lifting_hold_restores_entries_but_not_over_contour_stop(storage):
    env = Env(storage, msk(16, 11))
    env.manager.hold_entries(True)
    env.manager.hold_entries(False)
    env.signal(TF)
    assert len(env.planned) == 1
    assert _traces(storage) == [(CORE_LOST, "REJECTED"), (CORE_LOST, "ACCEPTED")]

    env.manager.stop_contour("no_entries")
    env.manager.hold_entries(True)
    env.manager.hold_entries(False)
    admission = env.signal(TF, event_id="second")
    assert [reason.code for reason in admission.rejections] == [CONTOUR_STOPPING]
