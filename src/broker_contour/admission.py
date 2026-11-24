"""Допуск контура на брокере к старту (add-sandbox-contour, design.md решение 6).

Порядок: токен брокера, бот и канал, счёт робота, отпечаток, чтение счёта.
Первые две проверки — без сети; отказ на остальных уходит тревогой в канал.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.broker_contour.fingerprint import FingerprintMismatch, check_fingerprint
from src.broker_contour.port import (AccountRuleError, AccountSnapshot, BrokerCallError, BrokerOutage, BrokerSession,
                                     RobotAccount)
from src.broker_contour.store import BrokerStore


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
    facts: dict | None = None        # факты допуска «Счёта» (add-account-contour); у песочницы нет


def check_settings(*, token: str | None, token_env: str, bot_token: str | None, bot_env: str,
                   channel_id: str | None, channel_env: str, account_id: str | None = None,
                   account_id_env: str | None = None) -> None:
    """Проверки без сети: токен брокера, номер счёта (у «Счёта»), бот и канал.

    Имя переменной в причине называется, значение — нет.
    """
    if not token:
        raise AdmissionError(f"Не задан {token_env}")
    if account_id_env is not None and not account_id:
        raise AdmissionError(f"Не задан {account_id_env}")
    if not bot_token:
        raise AdmissionError(f"Не задан {bot_env} — бот контура обязателен: сделки без уведомлений недопустимы")
    if not channel_id:
        raise AdmissionError(f"Не задан {channel_env} — канал контура обязателен: сделки без уведомлений недопустимы")


def admit_account(session: BrokerSession, store: BrokerStore, *, broker: str) -> Admitted:
    """Сетевые проверки: счёт робота, факты допуска (у «Счёта»), отпечаток, снимок счёта.

    Правило счёта и факты допуска — у сессии брокера; отказ фактов — ``AdmissionError``. Плановое окно
    брокера (``BrokerOutage``) — не отказ: оно уходит наружу, процесс ждёт конца окна.
    """
    try:
        account = session.find_account()
    except AccountRuleError as exc:
        raise AdmissionError(str(exc), alarm=True) from None
    except BrokerOutage:
        raise
    except BrokerCallError as exc:
        raise AdmissionError(f"Счёт брокера не прочитан: {exc.code}", alarm=True) from None
    facts = session.read_facts(account)
    try:
        _, created = check_fingerprint(store, broker, account.account_id)
    except FingerprintMismatch as exc:
        raise AdmissionError(str(exc), alarm=True, account_tail=account.masked) from None
    try:
        snapshot = session.read_account(account)
    except BrokerOutage:
        raise
    except BrokerCallError as exc:
        raise AdmissionError(f"Счёт брокера не прочитан: {exc.code}", alarm=True, account_tail=account.masked) from None
    return Admitted(account, snapshot, created, facts)
