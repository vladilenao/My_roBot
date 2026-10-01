from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.data.timeutil import to_naive


def system_now() -> datetime:
    """Системное время UTC без часового пояса."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_aware(moment: datetime) -> datetime:
    """Рыночный момент как tz-aware UTC: для ISO-строк журнала и границ API."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class Clock:
    """Порт рыночного времени.

    Боевой режим читает системные часы, исторический — виртуальные: время
    двигается только шагом ``advance()`` за тик, а пауза задаёт темп наблюдения.
    """

    is_virtual: bool = False

    def now(self) -> datetime:
        raise NotImplementedError

    def sleep(self, secs: float) -> None:
        if secs > 0:
            time.sleep(secs)

    def advance(self) -> None:
        return None


class SystemClock(Clock):
    """Рыночные часы боевого режима."""

    is_virtual = False

    def now(self) -> datetime:
        return system_now()


class FunctionClock(Clock):
    """Часы-обёртка над функцией времени (подстановка фиксированного момента)."""

    is_virtual = False

    def __init__(self, source: Callable[[], datetime]) -> None:
        self._source = source

    def now(self) -> datetime:
        return self._source()


class HistoricalClock(Clock):
    """Виртуальные рыночные часы прогона.

    ``now()`` — момент закрытия обрабатываемого бара: часы стартуют в начале
    диапазона, и каждый ``advance()`` сдвигает время на шаг, поэтому первый
    обрабатываемый бар открылся в ``start``, а последний — в ``end - step``.
    """

    is_virtual = True

    def __init__(
        self,
        start,
        end,
        step,
        pause_secs: float = 1.0,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        start_dt = to_naive(start).to_pydatetime()
        end_dt = to_naive(end).to_pydatetime()
        if end_dt <= start_dt:
            raise ValueError("Конец диапазона должен быть позже начала")
        if step is None or step <= timedelta(0):
            raise ValueError("Шаг рыночного времени должен быть положительным")
        self._start = start_dt
        self._end = end_dt
        self._step = step
        self._pause = max(0.0, float(pause_secs))
        self._sleeper = sleeper
        self._now = start_dt

    @property
    def start(self) -> datetime:
        return self._start

    @property
    def end(self) -> datetime:
        return self._end

    @property
    def step(self) -> timedelta:
        return self._step

    @property
    def pause_secs(self) -> float:
        return self._pause

    @property
    def finished(self) -> bool:
        """Достигнута ли правая граница диапазона."""
        return self._now >= self._end

    def now(self) -> datetime:
        return self._now

    def sleep(self, secs: float) -> None:
        return None

    def advance(self) -> None:
        """Следующий тик: шаг рыночного времени, затем пауза наблюдения."""
        if self._now >= self._end:
            return
        self._now += self._step
        if self._pause > 0:
            self._sleeper(self._pause)

    def ticks_total(self) -> int:
        """Сколько тиков (баров) укладывается в диапазон."""
        return int((self._end - self._start).total_seconds() // self._step.total_seconds())


def as_clock(clock: Clock | Callable[[], datetime] | None) -> Clock:
    """Приводит часы к порту: объект часов, функция времени или системные часы."""
    if clock is None:
        return SystemClock()
    if isinstance(clock, Clock):
        return clock
    if callable(clock):
        return FunctionClock(clock)
    now = getattr(clock, "now", None)
    if callable(now):
        return clock
    raise TypeError(f"Неизвестный источник рыночного времени: {clock!r}")
