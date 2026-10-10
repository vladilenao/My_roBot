"""Тихая сводка решений без торгового сигнала."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable



def idle_tick_summary(when: datetime, count: int, instruments: Iterable[str] = (), *, color: bool = False) -> str:
    """Число проверок отражает решения, а не уникальные инструменты."""
    remainder = count % 100
    if 11 <= remainder <= 14:
        word = "проверок"
    elif count % 10 == 1:
        word = "проверка"
    elif 2 <= count % 10 <= 4:
        word = "проверки"
    else:
        word = "проверок"
    line = f"{when:%H:%M}  СКАН       Сигналов нет · {count} {word}"
    return f"\x1b[90m{line}\x1b[0m" if color else line
