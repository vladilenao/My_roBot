"""Допуск контура на брокере к старту (add-sandbox-contour, design.md решение 6).

Порядок: токен брокера, бот и канал, счёт робота, отпечаток, чтение счёта.
Первые две проверки — без сети; отказ на остальных уходит тревогой в канал.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.broker_contour.account import AccountSnapshot, read_account
from src.broker_contour.fingerprint import FingerprintMismatch, check_fingerprint
from src.broker_contour.store import BrokerStore
from src.sandbox.account import AccountConflict, RobotAccount, robot_account
from src.sandbox.gateway import BrokerCallError


class AdmissionError(Exception):
    """Контур не стартует; ``alarm`` — причину нужно отправить в канал (канал уже есть)."""

    def __init__(self, reason: str, *, alarm: bool = False, account_tail: str = "…"):
        super().__init__(reason)
        self.reason = reason
        self.alarm = alarm
        self.account_tail = account_tail


@dataclass(frozen=True)
class Admitted:
    account: RobotAccount
    snapshot: AccountSnapshot
    fingerprint_created: bool


def check_settings(*, token: str | None, token_env: str, bot_token: str | None, bot_env: str,
                   channel_id: str | None, channel_env: str) -> None:
    """Проверки без сети: токен брокера, бот и канал. Имя переменной в причине называется, значение — нет."""
    if not token:
        raise AdmissionError(f"Не задан {token_env}")
    if not bot_token:
        raise AdmissionError(f"Не задан {bot_env} — бот контура обязателен: сделки без уведомлений недопустимы")
    if not channel_id:
        raise AdmissionError(f"Не задан {channel_env} — канал контура обязателен: сделки без уведомлений недопустимы")


def admit_account(gateway, store: BrokerStore, *, broker: str) -> Admitted:
    """Сетевые проверки: счёт робота, отпечаток, снимок счёта."""
    try:
        account = robot_account(gateway)
    except AccountConflict as exc:
        raise AdmissionError(str(exc), alarm=True) from None
    except BrokerCallError as exc:
        raise AdmissionError(f"Счёт брокера не прочитан: {exc}", alarm=True) from None
    try:
        _, created = check_fingerprint(store, broker, account.account_id)
    except FingerprintMismatch as exc:
        raise AdmissionError(str(exc), alarm=True, account_tail=account.masked) from None
    try:
        snapshot = read_account(gateway, account)
    except BrokerCallError as exc:
        raise AdmissionError(f"Счёт брокера не прочитан: {exc}", alarm=True, account_tail=account.masked) from None
    return Admitted(account, snapshot, created)
