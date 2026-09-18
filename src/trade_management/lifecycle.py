"""Canonical trade lifecycle values and user-facing labels."""

from __future__ import annotations

from enum import StrEnum


class LifecycleStatus(StrEnum):
    PLANNED = "PLANNED"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


LIFECYCLE_LABELS = {
    LifecycleStatus.PLANNED: "планируется",
    LifecycleStatus.ENTRY_PENDING: "вход ожидается",
    LifecycleStatus.OPEN: "открыта",
    LifecycleStatus.PARTIALLY_CLOSED: "частично закрыта",
    LifecycleStatus.CLOSED: "закрыта",
    LifecycleStatus.CANCELLED: "отменена",
    LifecycleStatus.REJECTED: "отклонена",
    LifecycleStatus.ERROR: "ошибка",
}

# BUILDING/REDUCING remain operational phases for the management policy; the
# user-facing lifecycle deliberately has one canonical status for both paths.
OPERATIONAL_TO_LIFECYCLE = {
    "BUILDING": LifecycleStatus.OPEN,
    "REDUCING": LifecycleStatus.PARTIALLY_CLOSED,
}

ALLOWED_TRANSITIONS = {
    LifecycleStatus.PLANNED: {LifecycleStatus.ENTRY_PENDING, LifecycleStatus.CANCELLED},
    LifecycleStatus.ENTRY_PENDING: {
        LifecycleStatus.OPEN, LifecycleStatus.CANCELLED, LifecycleStatus.REJECTED,
        LifecycleStatus.ERROR,
    },
    LifecycleStatus.OPEN: {LifecycleStatus.OPEN, LifecycleStatus.PARTIALLY_CLOSED, LifecycleStatus.CLOSED,
                           LifecycleStatus.ERROR},
    LifecycleStatus.PARTIALLY_CLOSED: {LifecycleStatus.PARTIALLY_CLOSED, LifecycleStatus.CLOSED,
                                       LifecycleStatus.ERROR},
    LifecycleStatus.CLOSED: set(),
    LifecycleStatus.CANCELLED: set(),
    LifecycleStatus.REJECTED: set(),
    LifecycleStatus.ERROR: set(),
}


def validate_transition(current: LifecycleStatus, next_status: LifecycleStatus) -> None:
    """Reject lifecycle regressions and transitions from terminal states."""
    if next_status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid lifecycle transition: {current} -> {next_status}")


def lifecycle_for_phase(phase: str, *, quantity: int | None = None) -> LifecycleStatus:
    """Translate durable operational phases into the public lifecycle."""
    raw = str(phase).upper()
    if raw in OPERATIONAL_TO_LIFECYCLE:
        return OPERATIONAL_TO_LIFECYCLE[raw]
    try:
        return LifecycleStatus(raw)
    except ValueError:
        if raw in {"REJECT", "REJECTED"}:
            return LifecycleStatus.REJECTED
        if raw in {"ERROR", "FAILED"}:
            return LifecycleStatus.ERROR
        return LifecycleStatus.OPEN if quantity else LifecycleStatus.PLANNED


def lifecycle_label(phase: str, *, quantity: int | None = None) -> str:
    return LIFECYCLE_LABELS[lifecycle_for_phase(phase, quantity=quantity)]
