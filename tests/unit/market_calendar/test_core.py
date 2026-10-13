from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from src.market_calendar import (
    CANDLE_RULES,
    CalendarDay,
    MarketPhase,
    SessionWindow,
    TradingCalendar,
    expect,
    expected_bars,
    scheduled_closed_eligible,
)


TIMEOUT = timedelta(seconds=65)
OBSERVED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def at(day: int, hour: int, minute: int = 0, second: int = 0, *, aware: bool = False) -> datetime:
    value = datetime(2026, 10, day, hour, minute, second)
    return value.replace(tzinfo=timezone.utc) if aware else value


def window(start: datetime, end: datetime, kind: str = "continuous") -> SessionWindow:
    return SessionWindow(start, end, kind, kind)


def calendar(*days: CalendarDay) -> TradingCalendar:
    return TradingCalendar("tbank", "moex_test", days)


def day(
    day_number: int,
    *windows: SessionWindow,
    is_trading_day: bool = True,
    version: str | None = None,
) -> CalendarDay:
    return CalendarDay(
        date(2026, 10, day_number),
        is_trading_day,
        tuple(windows),
        version or f"v-{day_number}",
        OBSERVED_AT,
    )


def test_calendar_normalizes_aware_inputs_to_utc_naive():
    item = day(13, window(at(13, 10, aware=True), at(13, 11, aware=True)))

    assert item.windows[0].start == at(13, 10)
    assert item.windows[0].start.tzinfo is None
    assert item.observed_at == OBSERVED_AT.replace(tzinfo=None)


def test_holiday_on_weekday_is_closed_without_weekday_heuristic():
    result = expect(calendar(day(14, is_trading_day=False)), CANDLE_RULES["1m"], at(14, 12), TIMEOUT)

    assert result.phase is MarketPhase.CLOSED
    assert result.next_event is None
    # Прошлых дат в календаре нет: последний бар прошлой сессии тоже неизвестен.
    assert result.reason == "next_session_unknown; previous_session_unknown"


def test_working_saturday_and_weekend_session_are_trading():
    saturday = day(17, window(at(17, 7), at(17, 12)))

    result = expect(calendar(saturday), CANDLE_RULES["1m"], at(17, 8), TIMEOUT)

    assert result.phase is MarketPhase.TRADING
    assert result.schedule is not None
    assert result.schedule.days == ((date(2026, 10, 17), "v-17"),)


def test_closed_before_known_next_session_reports_session_open():
    previous = day(12, is_trading_day=False)
    monday = day(13, window(at(13, 3, 50), at(13, 20, 50)))

    result = expect(calendar(previous, monday), CANDLE_RULES["1m"], at(13, 3), TIMEOUT)

    assert result.phase is MarketPhase.CLOSED
    assert result.next_event is not None
    assert result.next_event.kind == "session_open"
    assert result.next_event.at == at(13, 3, 50)


def test_monday_night_keeps_last_due_from_friday_until_known_open():
    friday = day(9, window(at(9, 20), at(9, 20, 49, 59)))
    weekend = day(10, is_trading_day=False)
    monday = day(12, window(at(12, 3, 50), at(12, 20, 50)))

    result = expect(calendar(friday, weekend, monday), CANDLE_RULES["1h"], at(12, 3), TIMEOUT)

    assert result.phase is MarketPhase.CLOSED
    assert result.last_due is not None
    assert (result.last_due.open, result.last_due.close) == (at(9, 20), at(9, 21))
    assert result.next_event is not None
    assert (result.next_event.kind, result.next_event.at) == ("session_open", at(12, 3, 50))


def test_missing_current_date_is_unknown_not_closed():
    result = expect(calendar(day(13, window(at(13, 10), at(13, 11)))), CANDLE_RULES["1m"], at(14, 10), TIMEOUT)

    assert result.phase is MarketPhase.UNKNOWN
    assert result.reason == "schedule_missing_date"
    assert result.next_event is None


def test_unknown_phase_does_not_guess_next_event_from_future_coverage():
    tomorrow = day(14, window(at(14, 10), at(14, 11)))

    result = expect(calendar(tomorrow), CANDLE_RULES["1m"], at(13, 10), TIMEOUT)

    assert result.phase is MarketPhase.UNKNOWN
    assert result.last_due is None
    assert result.next_event is None


def test_unknown_interval_is_trading_with_diagnostic():
    result = expect(
        calendar(day(13, window(at(13, 10), at(13, 11), "unknown"))),
        CANDLE_RULES["1m"],
        at(13, 10, 5),
        TIMEOUT,
    )

    assert result.phase is MarketPhase.TRADING
    assert result.reason == "unknown_interval_type"


def test_opening_auction_is_break_and_auction_minutes_are_not_expected():
    schedule = calendar(
        day(
            13,
            window(at(13, 9, 50), at(13, 10), "auction"),
            window(at(13, 10), at(13, 11)),
        )
    )

    result = expect(schedule, CANDLE_RULES["1m"], at(13, 9, 55), TIMEOUT)
    bars = expected_bars(schedule, CANDLE_RULES["1m"], at(13, 9, 50), at(13, 10, 2), TIMEOUT)

    assert (result.phase, result.break_kind) == (MarketPhase.BREAK, "auction")
    assert result.next_event is not None
    assert (result.next_event.kind, result.next_event.at) == ("break_end", at(13, 10))
    assert [bar.open.strftime("%H:%M") for bar in bars] == ["10:00", "10:01"]


def test_clearing_is_break_and_minute_bars_are_not_enumerated_inside_it():
    schedule = calendar(
        day(
            13,
            window(at(13, 13), at(13, 14)),
            window(at(13, 14), at(13, 14, 5), "clearing"),
            window(at(13, 14, 5), at(13, 15)),
        )
    )

    result = expect(schedule, CANDLE_RULES["1m"], at(13, 14, 2), TIMEOUT)
    bars = expected_bars(schedule, CANDLE_RULES["1m"], at(13, 13, 58), at(13, 14, 7), TIMEOUT)

    assert result.phase is MarketPhase.BREAK
    assert result.break_kind == "clearing"
    assert result.next_event is not None
    assert (result.next_event.kind, result.next_event.at) == ("break_end", at(13, 14, 5))
    assert [bar.open.strftime("%H:%M") for bar in bars] == ["13:58", "13:59", "14:05", "14:06"]


def test_bar_crossing_clearing_and_trading_stays_expected():
    schedule = calendar(
        day(
            13,
            window(at(13, 13), at(13, 14)),
            window(at(13, 14), at(13, 14, 5), "clearing"),
            window(at(13, 14, 5), at(13, 15)),
        )
    )

    bars = expected_bars(schedule, CANDLE_RULES["15m"], at(13, 14), at(13, 14, 30), TIMEOUT)

    assert [(bar.open, bar.close) for bar in bars] == [(at(13, 14), at(13, 14, 15)), (at(13, 14, 15), at(13, 14, 30))]


def test_last_due_closes_on_grid_after_session_end():
    # 21:05 UTC — уже 14-е по МСК: фазу даёт расписание 14-го.
    schedule = calendar(day(13, window(at(13, 20), at(13, 20, 49, 59))),
                        day(14, window(at(14, 4), at(14, 15))))

    result = expect(schedule, CANDLE_RULES["1h"], at(13, 21, 5), TIMEOUT)

    assert result.phase is MarketPhase.CLOSED
    assert result.last_due is not None
    assert (result.last_due.open, result.last_due.close) == (at(13, 20), at(13, 21))
    assert result.last_due.publication_deadline == at(13, 21, 1, 5)


def test_next_event_is_publication_deadline_for_last_closed_bar():
    schedule = calendar(day(13, window(at(13, 9, 59), at(13, 10))))

    result = expect(schedule, CANDLE_RULES["1m"], at(13, 10, 0, 10), TIMEOUT)

    assert result.last_due is not None
    assert result.next_event is not None
    assert (result.next_event.kind, result.next_event.at) == ("publication_deadline", at(13, 10, 1, 5))


def test_unconfirmed_rule_keeps_expectation_but_disables_calendar_evidence():
    schedule = calendar(day(13, window(at(13, 9, 59), at(13, 10))))
    rule = replace(CANDLE_RULES["1m"], rule_confirmed=False)

    result = expect(schedule, rule, at(13, 10, 0, 10), TIMEOUT)

    assert result.last_due is not None
    assert result.last_due.rule_confirmed is False
    assert "timeframe_rule_unconfirmed" in result.reason
    assert scheduled_closed_eligible(result, calendar_evidence=True) is False


def test_per_timeframe_timeout_sets_daily_publication_deadline():
    schedule = calendar(
        day(13, window(at(13, 10), at(13, 20))),
        day(14, is_trading_day=False),
    )

    result = expect(schedule, CANDLE_RULES["1d"], at(14, 0, 0, 10), timedelta(minutes=15))

    assert result.last_due is not None
    assert (result.last_due.open, result.last_due.close) == (at(13, 0), at(14, 0))
    assert result.last_due.publication_deadline == at(14, 0, 15)


def test_scheduled_closed_requires_evidence_and_confirmed_rule():
    schedule = calendar(day(13, window(at(13, 9, 59), at(13, 10))))
    result = expect(schedule, CANDLE_RULES["1m"], at(13, 10, 10), TIMEOUT)

    assert scheduled_closed_eligible(result, calendar_evidence=False) is False
    assert scheduled_closed_eligible(result, calendar_evidence=True) is True


def test_cross_midnight_weekend_window_uses_its_trading_date():
    weekend = day(17, window(at(16, 23), at(17, 6, 40)))

    result = expect(calendar(weekend), CANDLE_RULES["1m"], at(16, 23, 30), TIMEOUT)

    assert result.phase is MarketPhase.TRADING
    assert result.schedule is not None
    assert result.schedule.days == ((date(2026, 10, 17), "v-17"),)
