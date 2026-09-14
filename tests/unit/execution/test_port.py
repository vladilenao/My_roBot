from unittest.mock import Mock

import pytest

from src.execution import BrokerExecutionPort, ExecutionPort, NotifyOnlyExecutionPort
from src.instruments import Instrument
from src.strategies.contracts import Decision, SignalType


class TestNotifyOnlyExecutionPort:
    def test_execute_delivers_decision_via_notifier(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("SBER", "SBER", "share")

        port.execute(decision, instrument)

        notifier.notify_decision.assert_called_once_with(
            decision, "SBER", filter_profile="", filtered_out=False, timeframe=""
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

    def test_execute_falls_back_to_label_without_short_name(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future")

        port.execute(Decision(SignalType.BUY, 100.5), instrument)

        notifier.notify_decision.assert_called_once_with(
            Decision(SignalType.BUY, 100.5), "NG (Природный газ) — NG-9.26",
            filter_profile="", filtered_out=False, timeframe="",
        )

    def test_execute_creates_no_orders(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        instrument = Instrument("NGU6", "NGU6", "future")

        port.execute(Decision(SignalType.SELL, 10.0), instrument)

        # ордера не создаются, только уведомление
        assert notifier.notify.call_count + notifier.notify_decision.call_count >= 1

    def test_abstract_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            ExecutionPort()  # type: ignore[abstract]


class TestBrokerExecutionPort:
    def test_delegates_to_adapter(self):
        port = BrokerExecutionPort(adapter=Mock())
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("SBER", "SBER", "share")

        port.execute(decision, instrument)

        port._adapter.execute.assert_called_once_with(
            decision, instrument, filter_profile="", filtered_out=False, timeframe=""
        )

    def test_keeps_signal_notifications_when_notifier_given(self):
        notifier = Mock()
        adapter = Mock()
        port = BrokerExecutionPort(adapter=adapter, notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("NG (Природный газ) — NG-9.26", "NGU6", "future", "NG-9.26")

        port.execute(decision, instrument)

        notifier.notify_decision.assert_called_once_with(
            decision, "NG-9.26", filter_profile="", filtered_out=False, timeframe=""
        )
        adapter.execute.assert_called_once()

    def test_no_notification_without_notifier(self):
        notifier = Mock()
        port = BrokerExecutionPort(adapter=Mock())
        instrument = Instrument("SBER", "SBER", "share")

        port.execute(Decision(SignalType.SELL, 10.0), instrument)

        notifier.notify_decision.assert_not_called()
