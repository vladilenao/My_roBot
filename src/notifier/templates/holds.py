"""Фразы удержания рыночных данных для шаблонов каналов.

Слой анализа передаёт удержание структурой ``hold`` (вид и UTC-моменты);
каждый канал собирает фразу здесь, подставляя свой форматтер момента.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

UNKNOWN_SCHEDULE = "Применимое торговое расписание неизвестно"

Moment = Callable[[datetime], str]


def hold_key(hold: Any) -> tuple:
    """Удержание как сравнимый кортеж для ограничения повторов."""
    if not hold:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in hold.items()))


def _at(value: Any, moment: Moment) -> str:
    return moment(value) if value is not None else ""


def hold_phrase(hold: Any, moment: Moment) -> str:
    """Фраза удержания; неизвестный момент не выдумывается, а опускается."""
    if not hold:
        return ""
    kind = hold.get("kind")
    if kind == "execution_barrier":
        return f"Удержано: базовая 1m не подтверждена до {_at(hold.get('through'), moment)}".strip()
    if kind == "integrity":
        return f"Удержано: изменена уже исполненная минута {_at(hold.get('minute'), moment)}".strip()
    if kind == "conflict":
        return "Удержано: расхождение версий свечи, идёт согласующая проверка"
    if kind == "gap":
        span = f"{_at(hold.get('lower'), moment)}–{_at(hold.get('upper'), moment)}".strip("–")
        if hold.get("state") == "parked":
            retry = _at(hold.get("next_retry_at"), moment)
            return (("Пропуск " + span + " не восстановлен" if span else "Пропуск не восстановлен")
                    + (f"; следующая проверка {retry}" if retry else ""))
        return "Восстановление пропуска" + (" " + span if span else "")
    if kind == "reconciling":
        return "Согласование потока с историей"
    return ""


def data_reason(event: Any, moment: Moment) -> str:
    """Видимая причина состояния данных: удержание первым, затем ``reason``."""
    reason = event.get("reason") or ""
    phrase = hold_phrase(event.get("hold"), moment)
    if not phrase:
        return reason
    if reason == UNKNOWN_SCHEDULE:
        return phrase + "; расписание неизвестно"
    return phrase + ("; " + reason if reason else "")
