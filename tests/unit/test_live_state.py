from datetime import datetime, timedelta

from src.data.live_state import LivePairState, StatusCadence
from src.events.event import Event
from src.events.types import EventType, NOTIFICATION_EVENT_TYPES, TRADING_EVENT_TYPES


def test_deadline_survives_moving_clock_and_session_close():
    close = datetime(2026, 10, 9, 20)
    state = LivePairState("broker", "uid", "1h").observe(
        now=close, session="open", expected_bar=close, timeout_secs=60)
    state = state.observe(now=close + timedelta(hours=3), session="waiting_session",
                          expected_bar=close + timedelta(hours=2), timeout_secs=60)
    assert state.expected_bar == close
    assert state.publication_deadline == close + timedelta(seconds=60)
    assert state.data_state == "delayed"
    recovered = state.observe(now=close + timedelta(hours=3), session="waiting_session",
                             expected_bar=None, timeout_secs=60, received_bar=close)
    assert recovered.expected_bar is None
    assert recovered.data_state == "waiting_bar"


def test_progress_does_not_spam_status_but_recovery_transition_is_immediate():
    now = datetime(2026, 10, 12, 7)
    cadence = StatusCadence(60)
    state = LivePairState("broker", "uid", "1h", data_state="delayed")
    assert cadence.due(state, now)
    assert not cadence.due(state.with_analysis(now, mode="overview"), now + timedelta(seconds=1))
    current = state.observe(now=now, session="open", expected_bar=None, timeout_secs=60, received_bar=now)
    assert cadence.due(current, now + timedelta(seconds=2))
    assert not cadence.due(current, now + timedelta(seconds=59))
    assert cadence.due(current, now + timedelta(seconds=62))


def test_overview_snapshot_is_deeply_immutable_and_information_is_not_trading():
    result = {"assignment_id": "sber", "strategy": "macd_rsi_stoch", "filter_profile": "raw",
              "outcome": "BUY", "indicators": "RSI=60", "reason": ""}
    event = Event.startup_analysis("SBER", published_at=datetime(2026, 10, 12), results=[result],
                                   available_bars=100, required_bars=50, coverage_complete=True)
    result["outcome"] = "SELL"
    assert event.get("results")[0]["outcome"] == "BUY"
    assert {EventType.STARTUP_ANALYSIS, EventType.MARKET_DATA_STATUS} <= NOTIFICATION_EVENT_TYPES
    assert not {EventType.STARTUP_ANALYSIS, EventType.MARKET_DATA_STATUS} & TRADING_EVENT_TYPES


def test_shifted_hold_boundary_is_a_new_cause_but_same_hold_is_not():
    now = datetime(2026, 10, 15, 8)
    cadence = StatusCadence(900)
    held = LivePairState("broker", "uid", "15m", data_state="waiting_bar",
                         hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8)})
    assert cadence.due(held, now)
    assert not cadence.due(held, now + timedelta(seconds=5))
    shifted = LivePairState("broker", "uid", "15m", data_state="waiting_bar",
                            hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8, 15)})
    assert cadence.due(shifted, now + timedelta(seconds=10))
    assert shifted.payload(now)["hold"]["through"] == datetime(2026, 10, 15, 8, 15)
