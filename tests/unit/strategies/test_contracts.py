import pytest

from src.strategies.contracts import Assignment, Decision, SignalType


def test_assignment_requires_global_id_and_management_with_default_priority():
    assignment = Assignment(id="future-si-ma", strategy="ma_cloud_rsi_macd", management="ma_cloud", timeframe="5m")

    assert assignment.priority == 0
    assert assignment.id == "future-si-ma"
    assert assignment.management == "ma_cloud"
    with pytest.raises(TypeError):
        Assignment(strategy="ma_cloud_rsi_macd", timeframe="5m")


def test_decision_contains_only_entry_event_data():
    decision = Decision(SignalType.BUY, 101.0, event_id="entry-1", idea_references={"entry_reference": "high"})

    assert decision.event_id == "entry-1"
    assert not hasattr(decision, "stop_loss")
    assert not hasattr(decision, "quantity")
