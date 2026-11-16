"""Отпечаток счёта: журнал контура одного счёта не продолжается на другом."""

from __future__ import annotations

import hashlib

from src.broker_contour.store import BrokerStore, Fingerprint
from src.sandbox.records import mask_account


class FingerprintMismatch(Exception):
    """Найденный счёт не тот, на который записан журнал контура."""

    def __init__(self, recorded: Fingerprint):
        super().__init__(recorded)
        self.recorded = recorded

    def __str__(self) -> str:
        return (f"Журнал контура принадлежит другому счёту {self.recorded.account_tail} "
                f"({self.recorded.broker}). Чтобы начать с другим счётом, удалите папку контура.")


def fingerprint_of(broker: str, account_id: str) -> Fingerprint:
    return Fingerprint(broker, hashlib.sha256(account_id.encode("utf-8")).hexdigest(), mask_account(account_id))


def check_fingerprint(store: BrokerStore, broker: str, account_id: str) -> tuple[Fingerprint, bool]:
    """Сверить счёт с отпечатком; первого отпечатка нет — записать. ``(отпечаток, записан_сейчас)``."""
    found = fingerprint_of(broker, account_id)
    recorded = store.fingerprint()
    if recorded is None:
        store.save_fingerprint(found)
        return found, True
    if (recorded.broker, recorded.account_hash) != (found.broker, found.account_hash):
        raise FingerprintMismatch(recorded)
    return recorded, False
