"""Тихая сводка решений без торгового сигнала."""

from __future__ import annotations

from datetime import datetime
def idle_tick_summary(when: datetime, count: int, *, color: bool = False) -> str:
    """Число проверенных пар «инструмент + таймфрейм»."""
    remainder = count % 100
    if 11 <= remainder <= 14:
        word = "пар"
    elif count % 10 == 1:
        word = "пара"
    elif 2 <= count % 10 <= 4:
        word = "пары"
    else:
        word = "пар"
    line = f"{when:%H:%M}  СКАНИРОВАНИЕ  Сигналов нет · проверено {count} {word}"
    return f"\x1b[90m{line}\x1b[0m" if color else line
