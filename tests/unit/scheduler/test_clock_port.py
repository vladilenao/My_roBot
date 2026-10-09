from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.scheduler.clock import (
    Clock,
    FunctionClock,
    HistoricalClock,
    SystemClock,
    as_aware,
    as_clock,
    system_now,
)
from src.scheduler.timing import CandleScheduler, MultiTimeframeScheduler
from src.trade_journal.storage import Storage
from src.trade_management.actions import CancelEntry, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


def _utc(*args) -> datetime:
    return datetime(*args)


class _StubBroker:
    def register_trade(self, plan) -> None:
        return None


class TestSystemClock:
    def test_now_is_naive_utc(self):
        moment = SystemClock().now()

        assert moment.tzinfo is None
        assert abs((moment - system_now()).total_seconds()) < 5

    def test_is_not_virtual(self):
        assert SystemClock().is_virtual is False

    def test_advance_keeps_system_time(self):
        clock = SystemClock()

        before = clock.now()
        clock.advance()

        assert clock.now() >= before


class TestAsClock:
    def test_none_resolves_to_system(self):
        assert isinstance(as_clock(None), SystemClock)

    def test_callable_is_wrapped(self):
        moment = _utc(2024, 1, 1, 10, 0)

        clock = as_clock(lambda: moment)

        assert isinstance(clock, FunctionClock)
        assert clock.now() == moment
        assert clock.is_virtual is False

    def test_port_instance_passes_through(self):
        clock = HistoricalClock(_utc(2024, 1, 1), _utc(2024, 1, 2), timedelta(minutes=1), 0)

        assert as_clock(clock) is clock
        assert isinstance(clock, Clock)

    def test_unknown_source_rejected(self):
        with pytest.raises(TypeError):
            as_clock(object())


class TestAsAware:
    def test_naive_becomes_utc_aware(self):
        assert as_aware(_utc(2024, 1, 1, 10, 0)) == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

    def test_aware_is_converted_to_utc(self):
        moment = datetime(2024, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=3)))

        assert as_aware(moment) == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)


class TestSchedulerAcceptsPort:
    def test_candle_scheduler_reads_port_now(self):
        sched = CandleScheduler("1h", clock=SystemClock())

        assert sched.now().tzinfo is None
        assert sched.clock.is_virtual is False

    def test_multi_scheduler_boundaries_from_port(self):
        sched = MultiTimeframeScheduler(["1m", "1h"], clock=SystemClock())

        assert sched.timeframes == ("1m", "1h")
        assert sched.next_boundary() >= sched.now()
        assert sched.clock.is_virtual is False

    def test_callable_clock_still_accepted(self):
        sched = MultiTimeframeScheduler(["5m"], clock=lambda: _utc(2024, 1, 1, 10, 37))

        assert sched.now() == _utc(2024, 1, 1, 10, 37)
        assert sched.next_boundary() == _utc(2024, 1, 1, 10, 40)

    def test_default_clock_is_system(self):
        sched = MultiTimeframeScheduler(["1m"])

        assert sched.clock.is_virtual is False
        assert abs((sched.now() - system_now()).total_seconds()) < 5


class TestMarketClockInJournalWrites:
    def _plan(self) -> TradePlan:
        return TradePlan(
            "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
            (TargetPlan("tp-1", Decimal("104"), Decimal("1")),),
            ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}),
            datetime(2023, 6, 1, 14, 34, tzinfo=timezone.utc), "1m",
        )

    def test_outbox_stamp_uses_market_clock(self, tmp_path):
        clock = FunctionClock(lambda: _utc(2023, 6, 1, 14, 35))

        with Storage(tmp_path / "trades.sqlite3", clock=clock) as storage:
            manager = TradeManager(storage, _StubBroker(), clock=clock)
            manager.submit_plan(
                self._plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1)
            )
            stored = manager.submit_action(
                CancelEntry("cancel-1", "trade-1", 0, "test"), assignment_id="assignment-1"
            )
            row = storage.connection.execute(
                "SELECT created_at FROM outbox WHERE command_id = 'cancel-1'"
            ).fetchone()

        assert stored is True
        assert row[0].startswith("2023-06-01T14:35:00")

    def test_market_now_is_aware_utc(self, tmp_path):
        clock = FunctionClock(lambda: _utc(2023, 6, 1, 14, 35))

        with Storage(tmp_path / "trades.sqlite3", clock=clock) as storage:
            assert storage._now() == datetime(2023, 6, 1, 14, 35, tzinfo=timezone.utc)

    def test_trade_manager_clock_is_injectable(self, tmp_path):
        clock = FunctionClock(lambda: _utc(2023, 6, 1, 14, 35))

        with Storage(tmp_path / "trades.sqlite3", clock=clock) as storage:
            manager = TradeManager(storage, _StubBroker(), clock=clock)

            assert manager._clock.now() == _utc(2023, 6, 1, 14, 35)
            assert manager._clock.is_virtual is False

    def test_default_clock_keeps_system_time(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            assert abs((storage._now() - datetime.now(timezone.utc)).total_seconds()) < 5
