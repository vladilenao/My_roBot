"""Сигналы и недопуски по привязкам стратегий за исторический прогон.

Счёт ведётся там, где бот получает результат допуска сигнала, а не по
трассе решений: часть недопусков (``contract-expiring``,
``no-contract-metadata``) возвращается без записи в трассу.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

SCHEDULE_UNKNOWN = "session-schedule-unknown"


@dataclass
class _AssignmentStats:
    timeframe: str
    signals: set[str] = field(default_factory=set)
    admitted: set[str] = field(default_factory=set)
    rejections: dict[str, str] = field(default_factory=dict)  # signal_id -> первый код


class SignalAdmissionStats:
    """Накопитель прогона: уникальные сигналы, допущенные и первый недопуск каждого."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _AssignmentStats] = {}

    def record(self, assignment, timeframe: str, decision, admission) -> None:
        """Учитывает сигнал входа; HOLD (решение без сигнала) сигналом не считается."""
        signal_type = getattr(decision, "signal_type", None)
        if getattr(signal_type, "name", signal_type) == "HOLD":
            return
        signal_id = getattr(decision, "event_id", None) or ""
        with self._lock:
            item = self._items.setdefault(assignment.id, _AssignmentStats(timeframe or assignment.timeframe))
            item.signals.add(signal_id)
            if admission.actions or admission.plan is not None:
                item.admitted.add(signal_id)
            if admission.rejections and signal_id not in item.rejections:
                item.rejections[signal_id] = admission.rejections[0].code

    def rows(self, trades_by_assignment: dict[str, int]) -> tuple[StrategyAdmission, ...]:
        with self._lock:
            items = sorted(self._items.items())
            return tuple(
                StrategyAdmission(
                    assignment_id=assignment_id,
                    timeframe=item.timeframe,
                    signals=len(item.signals),
                    admitted=len(item.admitted),
                    trades=trades_by_assignment.get(assignment_id, 0),
                    rejections=_counts(item.rejections.values()),
                )
                for assignment_id, item in items
            )


def _counts(codes) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class StrategyAdmission:
    """Строка отчёта: привязка, сигналы, сделки и недопуски по кодам."""

    assignment_id: str
    timeframe: str
    signals: int
    admitted: int
    trades: int
    rejections: tuple[tuple[str, int], ...] = ()

    @property
    def schedule_unknown(self) -> int:
        return dict(self.rejections).get(SCHEDULE_UNKNOWN, 0)

    @property
    def blocked_by_schedule(self) -> bool:
        """Нет сделок, и не меньше половины сигналов отклонено из-за неизвестного расписания."""
        return self.trades == 0 and self.signals > 0 and self.schedule_unknown * 2 >= self.signals

    def to_json(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "timeframe": self.timeframe,
            "signals": self.signals,
            "admitted": self.admitted,
            "trades": self.trades,
            "rejections": dict(self.rejections),
        }


def trades_by_assignment(connection) -> dict[str, int]:
    """Сделки журнала по привязке."""
    rows = connection.execute(
        "SELECT assignment_id, COUNT(*) FROM trades GROUP BY assignment_id"
    ).fetchall()
    return {str(assignment_id): int(count) for assignment_id, count in rows}
