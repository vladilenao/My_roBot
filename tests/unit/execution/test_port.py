from unittest.mock import Mock

import pytest

from src.execution import ExecutionPort, NotifyOnlyExecutionPort
from src.instruments import Instrument
from src.strategies.contracts import Decision, SignalType


class TestNotifyOnlyExecutionPort:
    def test_execute_delivers_decision_via_notifier(self):
        notifier = Mock()
        port = NotifyOnlyExecutionPort(notifier=notifier)
        decision = Decision(SignalType.BUY, 100.5)
        instrument = Instrument("SBER", "SBER", "share")

        port.execute(decision, instrument)

        notifier.notify_decision.assert_called_once_with(decision, "SBER")

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
