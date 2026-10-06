from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.bot.run_session import (
    HistoricalRunSession,
    LiveRunSession,
    RunSession,
    build_run_session,
)
from src.data.cache import DataExhaustion, DataGap
from src.scheduler.clock import HistoricalClock, SystemClock


def _clock(start="2024-01-01 09:00", end="2024-01-01 10:00", step=5, pause=0.0):
    return HistoricalClock(
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        timedelta(minutes=step),
        pause,
        sleeper=lambda secs: None,
    )


def _session(**kw) -> HistoricalRunSession:
    return HistoricalRunSession(_clock(**kw))


class TestLiveRunSession:
    def test_never_stops(self):
        session = LiveRunSession()

        assert session.should_stop() is False
        assert session.stop_reason() == ""
        assert session.is_virtual is False

    def test_never_stops_after_many_checks(self):
        session = LiveRunSession()

        assert all(session.should_stop() is False for _ in range(100))

    def test_market_now_is_system_time(self):
        session = LiveRunSession()

        assert abs((session.market_now() - datetime.utcnow()).total_seconds()) < 5

    def test_is_run_session(self):
        assert isinstance(LiveRunSession(), RunSession)

    def test_base_session_never_stops(self):
        assert RunSession().should_stop() is False


class TestHistoricalRunSessionStopsAtRangeEnd:
    def test_runs_while_inside_range(self):
        session = _session()

        assert session.should_stop() is False
        assert session.stop_reason() == ""

    def test_stops_after_last_bar(self):
        session = _session()
        for _ in range(12):
            session._clock.advance()

        assert session.should_stop() is True
        assert session.stop_reason() == "диапазон [2024-01-01 09:00 — 2024-01-01 10:00) обработан полностью"

    def test_exposes_range(self):
        session = _session()

        assert session.start == datetime(2024, 1, 1, 9, 0)
        assert session.end == datetime(2024, 1, 1, 10, 0)
        assert session.is_virtual is True

    def test_counts_processed_ticks(self):
        session = _session()
        assert session.ticks_done() == 0
        session._clock.advance()
        session._clock.advance()

        assert session.ticks_done() == 2


class TestHistoricalRunSessionStopsOnDataEnd:
    def test_stops_when_data_exhausted_before_range_end(self):
        session = _session()
        for _ in range(3):
            session._clock.advance()

        session.mark_data_exhausted()

        assert session.should_stop() is True
        assert session.stop_reason() == (
            "данные закончились на 2024-01-01 09:15, "
            "до конца диапазона (2024-01-01 10:00) прогон не дошёл"
        )

    def test_first_signal_wins(self):
        session = _session()
        session._clock.advance()
        session.mark_data_exhausted()
        for _ in range(3):
            session._clock.advance()

        assert session.stop_reason().startswith("данные закончились на 2024-01-01 09:05")

    def test_marking_twice_keeps_first_moment(self):
        session = _session()
        session.mark_data_exhausted()
        session._clock.advance()
        session.mark_data_exhausted()

        assert session.stop_reason().startswith("данные закончились на 2024-01-01 09:00")

    def _exhaustion(self, horizon_end="2024-01-01 10:00", limited=False):
        return DataExhaustion(
            at=pd.Timestamp("2024-01-01 09:15"),
            horizon_start=pd.Timestamp("2024-01-01 09:15"),
            horizon_end=pd.Timestamp(horizon_end),
            range_end=pd.Timestamp("2024-01-01 10:00"),
            horizon_limited=limited,
        )

    def test_stop_reason_reports_horizon_checked_to_range_end(self):
        session = _session()
        for _ in range(3):
            session._clock.advance()
        session.mark_data_exhausted(self._exhaustion())

        assert session.stop_reason() == (
            "данные закончились на 2024-01-01 09:15; "
            "проверен весь остаток диапазона [2024-01-01 09:15 — 2024-01-01 10:00], "
            "до конца диапазона (2024-01-01 10:00) прогон не дошёл"
        )

    def test_stop_reason_reports_horizon_limited_by_lookahead(self):
        session = _session()
        for _ in range(3):
            session._clock.advance()
        session.mark_data_exhausted(
            self._exhaustion(horizon_end="2024-01-01 09:22", limited=True)
        )

        reason = session.stop_reason()

        assert (
            "следующий бар не найден в проверенном горизонте "
            "[2024-01-01 09:15 — 2024-01-01 09:22]" in reason
        )
        assert "их отсутствие не утверждается" in reason
        assert "диапазон обработан не полностью" in reason
        assert "2024-01-01 10:00" in reason

    def test_exhaustion_is_exposed_for_the_report(self):
        session = _session()
        assert session.exhaustion is None

        exhaustion = self._exhaustion()
        session.mark_data_exhausted(exhaustion)
        session.mark_data_exhausted(self._exhaustion(horizon_end="2024-01-01 10:00"))

        assert session.exhaustion is exhaustion


class TestHistoricalRunSessionCrossesDataGap:
    """Разрыв в данных — не конец прогона: часы переводятся на следующий бар."""

    def _gap(self, resume="2024-01-01 09:40"):
        return DataGap(
            since=pd.Timestamp("2024-01-01 09:12"),
            resume=pd.Timestamp(resume),
            missed=28,
            span=pd.Timedelta(minutes=28),
        )

    def test_market_time_moves_to_next_available_bar(self):
        session = _session()
        for _ in range(3):
            session._clock.advance()

        session.mark_data_gap(self._gap())

        assert session.market_now() == pd.Timestamp("2024-01-01 09:40")

    def test_gap_does_not_stop_the_run(self):
        session = _session()
        session._clock.advance()

        session.mark_data_gap(self._gap())

        assert session.should_stop() is False
        assert session.stop_reason() == ""

    def test_jump_never_crosses_range_end(self):
        session = _session()
        session._clock.advance()

        session.mark_data_gap(self._gap(resume="2024-01-01 23:00"))

        assert session.market_now() == pd.Timestamp("2024-01-01 10:00")
        assert session.should_stop() is True
        assert "обработан полностью" in session.stop_reason()

    def test_full_coverage_reported_only_at_range_end(self):
        session = _session()
        assert session.covered is False
        session._clock.advance()
        assert session.covered is False

        session.mark_data_gap(self._gap())

        assert session.covered is False

    def test_full_range_is_covered_when_clock_reached_end(self):
        session = _session()
        for _ in range(12):
            session._clock.advance()

        assert session.covered is True

    def test_exhausted_run_is_not_covered(self):
        session = _session()
        for _ in range(12):
            session._clock.advance()
        session.mark_data_exhausted()

        assert session.covered is False

    def test_jumps_are_counted(self):
        session = _session()
        session._clock.advance()
        session.mark_data_gap(self._gap())
        session.mark_data_gap(self._gap(resume="2024-01-01 09:50"))

        assert session.gaps_jumped == 2

    def test_live_session_ignores_gap(self):
        session = LiveRunSession(clock=lambda: datetime(2024, 1, 1, 9, 0))

        session.mark_data_gap(self._gap())

        assert session.should_stop() is False


class TestBuildRunSession:
    def test_live_mode_returns_live_session(self):
        session = build_run_session("live")

        assert isinstance(session, LiveRunSession)
        assert session.should_stop() is False

    def test_live_mode_accepts_system_clock(self):
        session = build_run_session("live", SystemClock())

        assert session.clock.is_virtual is False

    def test_history_mode_returns_historical_session(self):
        session = build_run_session("history", _clock())

        assert isinstance(session, HistoricalRunSession)
        assert session.is_virtual is True

    def test_history_mode_requires_virtual_clock(self):
        with pytest.raises(ValueError):
            build_run_session("history", SystemClock())

    def test_history_mode_requires_clock(self):
        with pytest.raises(ValueError):
            build_run_session("history")

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            build_run_session("replay")


class TestBotLoopStopsWithSession:
    def test_loop_exits_when_session_asks_to_stop(self):
        from src.bot.trading_bot import TradingBot

        bot = TradingBot(
            instruments=[],
            bus=MagicMock(),
            strategy_map={},
            data_cache=MagicMock(),
            timeline=MagicMock(),
            run=_session(),
        )
        bot._bootstrap = MagicMock(return_value=set())
        bot._tick = MagicMock(side_effect=lambda ready: bot._run._clock.advance())
        bot._validate = MagicMock()

        bot.run()

        assert bot._tick.call_count == 12
