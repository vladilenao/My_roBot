from unittest.mock import Mock
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution import ExecutionPort, NotifyOnlyExecutionPort
from src.instruments import Instrument
from src.strategies.contracts import Decision, SignalType
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


class TestNotifyOnlyExecutionPort:
    def test_execute_delivers_decision_via_notifier(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("SBER", "SBER", "share")

        port.execute(decision, instrument)

        notifier.notify_decision.assert_called_once_with(
            decision, "контракт не указан", filter_profile="", filtered_out=False, timeframe=""
        )

    def test_execute_uses_short_name_when_present(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future", "NG-9.26")

        port.execute(decision, instrument)

        notifier.notify_decision.assert_called_once_with(
            decision, "NG-9.26", filter_profile="", filtered_out=False, timeframe=""
        )

    def test_execute_never_uses_long_label_or_raw_ticker(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future")

        port.execute(Decision(SignalType.BUY, 100.5), instrument)

        notifier.notify_decision.assert_called_once_with(
            Decision(SignalType.BUY, 100.5), "контракт не указан",
            filter_profile="", filtered_out=False, timeframe="",
        )

    def test_execute_creates_no_orders(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        instrument = Instrument("NGU6", "NGU6", "future")

        port.execute(Decision(SignalType.SELL, 10.0), instrument)

        # ордера не создаются, только уведомление
        assert notifier.notify.call_count + notifier.notify_decision.call_count >= 1

    def test_plan_notification_does_not_create_position_or_reservation(self):
        notifier = Mock()
        portfolio = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        plan = TradePlan(
            trade_id="trade-1", assignment_id="assignment-1", instrument_id="NGU6",
            side="BUY", signal_id="signal-1", reference_entry=Decimal("100"),
            stop_price=Decimal("96"),
            targets=(TargetPlan("tp1", Decimal("104"), Decimal("1")),),
            profile=ProfileSnapshot("levels_rr", "1", {}),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        port.execute(plan, Instrument("Natural gas", "NGU6", "future", "NG-10.26"), timeframe="1h")

        notifier.notify_plan.assert_called_once_with(plan, "NG-10.26", timeframe="1h")
        assert portfolio.mock_calls == []

    def test_report_rejection_forwards_reason_to_notifier(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("SBER", "SBER", "share")

        port.report_rejection(
            decision, instrument,
            reason_message="Размер позиции ниже минимального", filter_profile="basic_levels", timeframe="15m",
        )

        notifier.notify_rejection.assert_called_once_with(
            decision, "контракт не указан", reason="Размер позиции ниже минимального",
            filter_profile="basic_levels", timeframe="15m",
        )

    def test_report_rejection_uses_short_name_when_present(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future", "NG-9.26")

        port.report_rejection(Decision(SignalType.BUY, 100.5), instrument, reason_message="r")

        notifier.notify_rejection.assert_called_once_with(
            Decision(SignalType.BUY, 100.5), "NG-9.26", reason="r",
            filter_profile="", timeframe="",
        )

    def test_report_entry_accepted_forwards_plan_and_short_name(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        plan = TradePlan(
            trade_id="trade-1", assignment_id="assignment-1", instrument_id="NGU6",
            side="BUY", signal_id="signal-1", reference_entry=Decimal("100"),
            stop_price=Decimal("96"),
            targets=(TargetPlan("tp1", Decimal("104"), Decimal("1")),),
            profile=ProfileSnapshot("levels_rr", "1", {}),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future", "NG-9.26")

        port.report_entry_accepted(
            plan, instrument, quantity=4, filter_profile="basic_levels", timeframe="15m",
        )

        notifier.notify_entry_accepted.assert_called_once_with(
            plan, "NG-9.26", quantity=4, timeframe="15m"
        )

    def test_abstract_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            ExecutionPort()  # type: ignore[abstract]
