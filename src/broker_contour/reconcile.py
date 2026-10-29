"""Сверщик счёта с журналом контура (add-sandbox-contour, design.md решение 8).

По каждому инструменту сравниваются позиция в лотах, число активных заявок и
стоп-заявок. Уведомления — только на переходах: появилось расхождение или
изменился его состав — тревога; расхождений не стало — «устранено».
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from src.broker_contour.account import AccountSnapshot
from src.logging_setup import get_logger

log = get_logger(__name__)

OK, MISMATCH, SKIPPED = "ok", "mismatch", "skipped"
UNREACHABLE_ALARM_AFTER = 3


@dataclass(frozen=True)
class JournalView:
    """Что контур сам держит на счёте. На С2 своих сделок нет — журнал пуст."""

    holdings: tuple[tuple[str, int], ...] = ()
    orders: tuple[str, ...] = ()
    stop_orders: tuple[str, ...] = ()


def compare(account: AccountSnapshot, journal: JournalView) -> list[dict]:
    """Расхождения ``{instrument, what, account, journal}`` по инструментам, в порядке имени."""
    sides = (
        ("position", dict(account.holdings), dict(journal.holdings)),
        ("order", Counter(account.orders), Counter(journal.orders)),
        ("stop_order", Counter(account.stop_orders), Counter(journal.stop_orders)),
    )
    found = []
    for what, theirs, ours in sides:
        for name in sorted(set(theirs) | set(ours)):
            if theirs.get(name, 0) != ours.get(name, 0):
                found.append({"instrument": name, "what": what, "account": theirs.get(name, 0),
                              "journal": ours.get(name, 0)})
    return sorted(found, key=lambda item: (item["instrument"], item["what"]))


class Reconciler:
    """Состояние сверки одного процесса: заморозки, последний состав расхождений, пропуски."""

    def __init__(self, *, store, notifier, account_tail: str, every_secs: float):
        self._store = store
        self._notifier = notifier
        self._account = account_tail
        self._every = every_secs
        self.frozen: tuple[str, ...] = ()
        self.last_at: datetime | None = None
        self.skipped_in_row = 0
        self._signature: tuple | None = ()  # () — «совпадает»; при старте процесса всё начинается заново

    def due(self, now: datetime) -> bool:
        return self.last_at is None or (now - self.last_at).total_seconds() >= self._every

    def run(self, now: datetime, snapshot: AccountSnapshot | None, journal: JournalView = JournalView()) -> dict:
        """Одна сверка; ``snapshot=None`` — счёт не прочитан, сверка пропущена."""
        self.last_at = now
        stamp = now.isoformat().replace("+00:00", "Z")
        if snapshot is None:
            self.skipped_in_row += 1
            report = {"at": stamp, "account": self._account, "result": SKIPPED, "items": [],
                      "frozen": list(self.frozen), "skipped_in_row": self.skipped_in_row}
            if self.skipped_in_row == UNREACHABLE_ALARM_AFTER:
                self._notifier.alarm([], self.frozen, reason="Счёт недоступен: три сверки подряд пропущены.")
            self._store.add_report(report)
            return report
        self.skipped_in_row = 0
        items = compare(snapshot, journal)
        frozen = tuple(sorted({item["instrument"] for item in items}))
        signature = tuple((item["instrument"], item["what"], item["account"], item["journal"]) for item in items)
        if signature != self._signature:
            if items:
                log.warning("Сверка счёта: расхождение по %s", ", ".join(frozen))
                self._notifier.alarm(items, frozen)
            else:
                log.info("Сверка счёта: расхождение устранено, разморожены %s", ", ".join(self.frozen) or "—")
                self._notifier.cleared(self.frozen)
        self._signature, self.frozen = signature, frozen
        report = {"at": stamp, "account": self._account, "result": MISMATCH if items else OK, "items": items,
                  "frozen": list(frozen), "skipped_in_row": 0}
        self._store.add_report(report)
        return report
