"""Чистая сессионная политика на расписании брокера 16–19.10.2026."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from src.market_calendar import CalendarDay, SessionWindow, TradingCalendar
from src.trade_management.intraday_settings import IntradaySettings
from src.trade_management.models import (
    CURRENT_ALGORITHM, CostSnapshot, EntryOrderType, ProfileSnapshot, TargetPlan, TradePhase, TradePlan, TradeState,
)
from src.trade_management.session_guard import (
    CLOSE_NEAR, SCHEDULE_UNKNOWN, SESSION_CLOSED, carry_decision, day_schedule, entry_decision,
    holding_decision,
)
from tests.support.session_schedule import FUTURES, OBSERVED_AT, SHARES, msk

DEFAULT = IntradaySettings()


def test_blocks_merge_morning_and_main_and_round_end_up():
    shares = day_schedule(SHARES, date(2026, 10, 16), DEFAULT)
    futures = day_schedule(FUTURES, date(2026, 10, 16), DEFAULT)
    assert [(b.start, b.end) for b in shares.blocks] == [(msk(16, 7), msk(16, 18, 55))]
    assert [(b.start, b.end) for b in futures.blocks] == [(msk(16, 7), msk(16, 19))]


@pytest.mark.parametrize("calendar,cutoff,flatten", [
    (SHARES, msk(16, 17, 55), msk(16, 18, 35)),
    (FUTURES, msk(16, 18), msk(16, 18, 40)),
])
def test_cutoffs_of_main_session(calendar, cutoff, flatten):
    block = day_schedule(calendar, date(2026, 10, 16), DEFAULT).blocks[0]
    assert block.entry_cutoff(DEFAULT) == cutoff
    assert block.flatten_at(DEFAULT) == flatten


def test_closing_auction_does_not_extend_share_block():
    assert entry_decision(SHARES, msk(16, 18, 56), DEFAULT).code == SESSION_CLOSED


@pytest.mark.parametrize("moment,code", [
    (msk(16, 7, 16), None),
    (msk(16, 8, 30), None),
    (msk(16, 17, 55), None),
    (msk(16, 18), CLOSE_NEAR),
    (msk(16, 18, 59), CLOSE_NEAR),
    (msk(16, 19, 5), SESSION_CLOSED),
    (msk(15, 23, 10), SESSION_CLOSED),
    (msk(16, 6, 55), SESSION_CLOSED),
    (msk(17, 12), SESSION_CLOSED),
])
def test_futures_entry_decisions(moment, code):
    assert entry_decision(FUTURES, moment, DEFAULT).code == code


def test_share_cutoff_is_five_minutes_earlier():
    assert entry_decision(SHARES, msk(16, 17, 54), DEFAULT).allowed
    assert entry_decision(SHARES, msk(16, 17, 55), DEFAULT).code == CLOSE_NEAR


def test_weekend_session_when_allowed():
    settings = IntradaySettings(entry_sessions=("morning", "main", "weekend"))
    assert entry_decision(FUTURES, msk(17, 12), settings).allowed
    assert entry_decision(FUTURES, msk(17, 18, 5), settings).code == CLOSE_NEAR


def test_main_and_evening_form_one_block_until_midnight():
    settings = IntradaySettings(entry_sessions=("main", "evening"))
    block = day_schedule(FUTURES, date(2026, 10, 16), settings).blocks[0]
    assert (block.start, block.end) == (msk(16, 9), msk(16, 23, 50))
    assert entry_decision(FUTURES, msk(16, 22), settings).allowed


def test_empty_session_list_never_admits():
    assert entry_decision(FUTURES, msk(16, 12), IntradaySettings(entry_sessions=())).code == SESSION_CLOSED


def test_missing_day_is_unknown():
    assert entry_decision(FUTURES, msk(20, 12), DEFAULT).code == SCHEDULE_UNKNOWN
    assert entry_decision(None, msk(16, 12), DEFAULT).code == SCHEDULE_UNKNOWN


def test_non_trading_day_and_untyped_windows_are_unknown():
    closed = TradingCalendar("tbank", "x", (CalendarDay(date(2026, 10, 16), False, (), "v", OBSERVED_AT),))
    untyped = TradingCalendar("tbank", "x", (CalendarDay(
        date(2026, 10, 16), True,
        (SessionWindow(msk(16, 7), msk(16, 23), "continuous", "regular_trading_session"),
         SessionWindow(msk(16, 12), msk(16, 13), "unknown", "brand_new_session")),
        "v", OBSERVED_AT),))
    assert entry_decision(closed, msk(16, 12), DEFAULT).code == SCHEDULE_UNKNOWN
    assert entry_decision(untyped, msk(16, 12), DEFAULT).code == SCHEDULE_UNKNOWN


def test_stale_calendar_is_used_as_known():
    stale = replace(FUTURES, stale=True)
    assert entry_decision(stale, msk(16, 12), DEFAULT).allowed


@pytest.mark.parametrize("moment,obligated", [
    (msk(16, 12), False),
    (msk(16, 18, 39), False),
    (msk(16, 18, 40), True),
    (msk(16, 20, 15), True),
    (msk(17, 12), True),
    (msk(16, 6, 30), True),
])
def test_holding_obligation_outside_safe_window(moment, obligated):
    assert holding_decision(FUTURES, moment, DEFAULT).obligated is obligated


def test_unknown_schedule_creates_no_obligation():
    assert holding_decision(FUTURES, msk(20, 18, 45), DEFAULT).obligated is False


def test_carry_approval_lasts_until_next_flatten():
    friday = holding_decision(FUTURES, msk(16, 18, 40), DEFAULT)
    assert friday.last_flatten == msk(16, 18, 40)
    approved = msk(16, 18, 41)
    assert holding_decision(FUTURES, msk(17, 12), DEFAULT).carry_valid(approved)
    assert holding_decision(FUTURES, msk(19, 8), DEFAULT).obligated is False
    assert not holding_decision(FUTURES, msk(19, 18, 40), DEFAULT).carry_valid(approved)


def _plan(side="BUY", profile="levels_rr", average=D("3442.5")):
    sign = 1 if side == "BUY" else -1
    return TradePlan(
        "ydex", "share-ydex-macd-15m", "YDEX", side, "signal", average, average - sign * 18,
        (TargetPlan("tp-1", average + sign * 24, D("0.5")), TargetPlan("tp-2", average + sign * 48, D("0.5"))),
        ProfileSnapshot(profile, "1", {}), msk(16, 7, 15), timeframe="15m",
        algorithm_version=CURRENT_ALGORITHM, cost_snapshot=CostSnapshot(D("1.5"), D(1)),
        entry_order_type=EntryOrderType.LIMIT,
    )


def _state(*, stop=D("3422.5"), targets=frozenset({"tp-1"}), realized=D(120), average=D("3442.5")):
    return TradeState(
        "ydex", TradePhase.REDUCING, 3, quantity=5, average_price=average, completed_target_ids=targets,
        confirmed_stop=stop, initial_stop_distance=D(20), max_quantity=10, realized_pnl=realized,
        fees=D("22.5"), fees_known=True,
    )


def _carry(plan, state, close, settings=DEFAULT):
    return carry_decision(plan, state, close=close, price_step=D("0.5"), step_cost=D("0.5"), settings=settings)


def test_ydex_stop_moves_to_whole_trade_break_even():
    decision = _carry(_plan(), _state(), D(3450))
    assert (decision.action, decision.break_even) == ("move", D("3425.0"))


def test_market_below_break_even_closes():
    assert _carry(_plan(), _state(), D(3424)).action == "close"


def test_stop_already_at_break_even_keeps():
    assert _carry(_plan(), _state(stop=D("3425.0")), D(3424)).action == "keep"


def test_no_first_target_closes_even_in_profit():
    decision = _carry(_plan(), _state(targets=frozenset(), realized=D(0)), D(3500))
    assert (decision.action, decision.reason) == ("close", "no-first-fixation")


def test_carry_disabled_always_closes():
    assert _carry(_plan(), _state(), D(3450), IntradaySettings(keep_if_tp1_and_breakeven=False)).action == "close"


def test_unknown_costs_close():
    state = replace(_state(), fees_known=False)
    assert _carry(_plan(), state, D(3450)).reason == "costs-unknown"


def test_ma_cloud_partial_exit_counts_as_first_fixation():
    plan = replace(_plan(profile="ma_cloud"), targets=())
    assert _carry(plan, _state(targets=frozenset()), D(3450)).action == "move"
    assert _carry(plan, _state(targets=frozenset(), realized=D(-5)), D(3450)).action == "close"


def test_short_is_mirrored():
    plan = _plan(side="SELL", average=D("3442.5"))
    state = _state(stop=D("3462.5"))
    decision = _carry(plan, state, D(3430))
    assert (decision.action, decision.break_even) == ("move", D("3460.0"))
    assert _carry(plan, state, D(3461)).action == "close"
