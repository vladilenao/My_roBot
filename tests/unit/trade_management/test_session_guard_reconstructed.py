"""Сессионная защита по восстановленному расписанию эмулятора (add-reconstructed-history-schedule).

Расписание — восстановленные дни 17–19.04 и 01.05.2025 из фикстуры ответа эмулятора,
пропущенные через настоящие ``EmulatorScheduleSource`` и ``MarketCalendar``.
В 2025 году основная сессия FORTS шла до 18:50, вечерняя — с 19:05.
"""

import json
from datetime import datetime, timedelta

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade
from tests.unit.market_calendar.test_reconstructed_schedule import RULES, fixture_days, loaded
from tests.unit.trade_management.test_intraday_session_guard import Env

MSK = timedelta(hours=3)


def msk(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2025, month, day, hour, minute) - MSK


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as opened:
        yield opened


def session_calendar(*, allow=True):
    calendar, _, _ = loaded(fixture_days(), allow=allow)
    return calendar.calendar_for("uid", msk(4, 18, 12), include_reconstructed=True)


def rejections(env):
    return [reason.code for reason in env.signal().rejections]


def test_entry_in_main_session_is_allowed_and_traced_as_reconstructed(storage):
    env = Env(storage, msk(4, 18, 12), calendar=session_calendar())

    assert rejections(env) == []
    assert env.planned


@pytest.mark.parametrize("moment, code", [
    (msk(4, 18, 17, 55), "intraday-close-near"),
    (msk(4, 18, 19, 30), "intraday-session-closed"),
    (msk(4, 19, 12), "session-schedule-unknown"),  # суббота до сессий выходного дня
    (msk(5, 1, 12), "session-schedule-unknown"),   # праздник
])
def test_rejections_follow_reconstructed_windows(storage, moment, code):
    env = Env(storage, moment, calendar=session_calendar())

    assert rejections(env) == [code]
    ((_, inputs),) = env.traces(code)
    payload = json.loads(inputs)
    assert payload["schedule_provenance"]["value"] == "reconstructed"
    assert payload["schedule_rules_version"]["value"] == RULES


def test_entry_cutoff_is_hour_before_main_session_end(storage):
    env = Env(storage, msk(4, 18, 17, 49), calendar=session_calendar())
    assert rejections(env) == []

    env.now = msk(4, 18, 17, 50)
    assert rejections(env) == ["intraday-close-near"]
    ((_, inputs),) = env.traces("intraday-close-near")
    assert json.loads(inputs)["entry_cutoff"]["value"] == msk(4, 18, 17, 50).isoformat()


def test_position_is_closed_at_1830_by_reconstructed_day(storage):
    env = Env(storage, msk(4, 18, 12), calendar=session_calendar())
    env.open()
    env.now = msk(4, 18, 18, 29)
    assert env.manage() == ()

    env.now = msk(4, 18, 18, 30)
    (close,) = env.manage()

    assert isinstance(close, CloseTrade) and close.reason == "intraday-session-end"


def test_disabled_reconstruction_means_unknown_schedule(storage):
    env = Env(storage, msk(4, 18, 12), calendar=session_calendar(allow=False))

    assert rejections(env) == ["session-schedule-unknown"]
    ((_, inputs),) = env.traces("session-schedule-unknown")
    payload = json.loads(inputs)
    assert payload["schedule_provenance"]["value"] is None
    assert payload["schedule_rules_version"]["value"] is None


def test_trace_algorithm_version_is_2(storage):
    env = Env(storage, msk(4, 18, 19, 30), calendar=session_calendar())
    env.signal()

    (version,) = storage.connection.execute(
        "SELECT algorithm_version FROM calculations WHERE algorithm='session.intraday'"
    ).fetchone()
    assert version == "2"
