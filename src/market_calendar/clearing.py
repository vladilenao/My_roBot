"""Сверка окон клиринга из расписания с ``[trading] clearing_times``.

Только диагностика: ``JournalBroker`` по-прежнему проводит клиринг по
конфигу (non-goal change). Расхождение у FORTS ежедневное — в ответе брокера
нет дневного клиринга (проба 8.5), поэтому сообщается раз на дату и версию.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Iterable

from .core import CalendarDay
from .intervals import EXCHANGE_UTC_OFFSET

CLEARING_TOLERANCE = timedelta(minutes=5)


def _config_instants(day: CalendarDay, clearing_times_msk: Iterable[str]) -> list[datetime]:
    result = []
    for text in clearing_times_msk:
        hours, minutes = (int(part) for part in str(text).split(":"))
        result.append(datetime.combine(day.trading_date, time(hours, minutes)) - EXCHANGE_UTC_OFFSET)
    return sorted(result)


def _msk(value: datetime) -> str:
    return (value + EXCHANGE_UTC_OFFSET).strftime("%H:%M")


def clearing_divergence(day: CalendarDay, clearing_times_msk: Iterable[str],
                        tolerance: timedelta = CLEARING_TOLERANCE) -> str | None:
    """Текст расхождения или ``None``, если окна и моменты конфига совпадают.

    Момент конфига совпадает с окном, если попадает в него с допуском;
    окно — если в него с тем же допуском попадает момент конфига.
    """
    if not day.is_trading_day:
        return None
    windows = [window for window in day.windows if window.kind == "clearing"]
    instants = _config_instants(day, clearing_times_msk)

    def covered(instant, window):
        return window.start - tolerance <= instant <= window.end + tolerance

    lonely_instants = [moment for moment in instants if not any(covered(moment, window) for window in windows)]
    lonely_windows = [window for window in windows if not any(covered(moment, window) for moment in instants)]
    if not lonely_instants and not lonely_windows:
        return None
    scheduled = ", ".join(f"{_msk(window.start)}–{_msk(window.end)}" for window in windows) or "нет"
    configured = ", ".join(_msk(moment) for moment in instants) or "нет"
    return (f"окна клиринга по расписанию (МСК): {scheduled}; "
            f"[trading] clearing_times: {configured}")
