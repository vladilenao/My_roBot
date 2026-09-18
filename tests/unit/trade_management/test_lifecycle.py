import pytest

from src.trade_management.lifecycle import (
    LIFECYCLE_LABELS,
    LifecycleStatus,
    lifecycle_for_phase,
    validate_transition,
)


def test_all_lifecycle_codes_have_russian_labels():
    assert set(LIFECYCLE_LABELS) == set(LifecycleStatus)
    assert LIFECYCLE_LABELS[LifecycleStatus.PARTIALLY_CLOSED] == "частично закрыта"
    assert LIFECYCLE_LABELS[LifecycleStatus.REJECTED] == "отклонена"
    assert LIFECYCLE_LABELS[LifecycleStatus.ERROR] == "ошибка"


def test_operational_phases_map_to_public_lifecycle():
    assert lifecycle_for_phase("BUILDING") is LifecycleStatus.OPEN
    assert lifecycle_for_phase("REDUCING") is LifecycleStatus.PARTIALLY_CLOSED


def test_terminal_lifecycle_transition_is_rejected():
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        validate_transition(LifecycleStatus.CLOSED, LifecycleStatus.OPEN)
