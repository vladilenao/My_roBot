from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

_PERIODS = {
    "1m": ("minute", 1),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "1d": ("day", 1),
    "1w": ("week", 1),
    "1M": ("month", 1),
}


def _month_span(t: datetime) -> timedelta:
    """Длительность месяца как смещение от первого числа к первому следующего."""
    if t.month == 12:
        next_first = datetime(t.year + 1, 1, 1)
    else:
        next_first = datetime(t.year, t.month + 1, 1)
    this_first = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return next_first - this_first


class CandleScheduler:
    """Выравнивает итерацию бота по границе закрытия свечи таймфрейма.

    Границы считаются по календарной сетке таймфрейма в UTC, а не от момента
    запуска. ``clock`` инжектируется для возможности подстановки фиктивного
    времени в тестах.
    """

    def __init__(
        self,
        timeframe: str,
        sleep_secs: float = 3600.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    ) -> None:
        self._timeframe = timeframe
        self._fallback = sleep_secs
        self._clock = clock
        try:
            self._unit, self._step = _PERIODS[timeframe]
        except KeyError:
            raise ValueError(
                f"Неподдерживаемый таймфрейм '{timeframe}'. "
                f"Доступны: {', '.join(sorted(_PERIODS))}"
            ) from None

    def now(self) -> datetime:
        """Текущее рыночное время (UTC)."""
        return self._clock()

    @property
    def timeframe(self) -> str:
        return self._timeframe

    def current_candle_start(self, t: datetime | None = None) -> datetime:
        """Момент начала текущей (ещё не закрытой) свечи."""
        current = (t or self.now())
        return self._floor(current)

    def previous_candle_start(self, t: datetime | None = None) -> datetime:
        """Момент начала свечи, закрывающейся на ближайшей границе.

        Для текущего момента -- начало только что закрывшегося бара
        (``current_candle_start - period``).
        """
        current = (t or self.now())
        start = self._floor(current)
        return start - self._period(start)


    def next_candle_close(self, t: datetime | None = None) -> datetime:
        """Момент начала следующей свечи (= конец текущей)."""
        current = (t or self.now())
        start = self._floor(current)
        return start + self._period(start)

    def wait_until_candle_close(self) -> datetime:
        """Блокирующе ждёт до границы закрытия текущей свечи, возвращает границу."""
        target = self.next_candle_close()
        delay = (target - self.now()).total_seconds()
        if delay > 0:
            self._sleep(delay)
        return target

    def wait_until_bar_published(
        self,
        bar_ready: Callable[[], bool],
        poll_secs: float = 1.0,
        timeout_secs: float = 65.0,
        wait_boundary: bool = True,
    ) -> None:
        """Блокирующе ждёт до границы закрытия свечи и появления свежего закрытого бара.

        При ``wait_boundary=True`` сначала ожидает наступления календарной границы
        закрытия текущей свечи. Затем повторно вызывает ``bar_ready()`` с паузами
        ``poll_secs``, пока она не вернёт ``True``. Останавливается по истечении
        ``timeout_secs`` (измеряется через ``time.monotonic``), предотвращая
        вечную блокировку при недоступности свежего бара.

        При ``wait_boundary=False`` ожидание границы пропускается: сразу начинается
        ограниченный опрос появившегося закрытого бара (используется на первом тике
        при запуске, когда граница уже пройдена или ещё не наступила).
        """
        if wait_boundary:
            target = self.next_candle_close()
            delay = (target - self.now()).total_seconds()
            if delay > 0:
                self._sleep(delay)
        deadline = time.monotonic() + timeout_secs
        while not bar_ready():
            if time.monotonic() >= deadline:
                break
            self._sleep(poll_secs)

    def fallback_secs(self) -> float:
        """Пауза при внешнем сбое — не ждать до свечи, но и не долбить API."""
        return self._fallback

    def bar_close(self, bar_start: datetime) -> datetime:
        """Момент закрытия бара, начавшегося в ``bar_start`` (начало + длительность периода).

        Используется для отображения времени бара по моменту закрытия свечи.
        Поддерживает переменные периоды (неделя и месяц).
        """
        return bar_start + self._period(bar_start)

    def _sleep(self, secs: float) -> None:
        """Приостанавливает поток. Выделено для подстановки в тестах."""
        time.sleep(secs)

    def as_timestamp(self, when: datetime) -> pd.Timestamp:
        return pd.Timestamp(when)

    # ── внутренняя математика сетки ──
    def _period(self, t: datetime) -> timedelta:
        unit = self._unit
        step = self._step
        if unit == "minute":
            return timedelta(minutes=step)
        if unit == "hour":
            return timedelta(hours=step)
        if unit == "day":
            return timedelta(days=step)
        if unit == "week":
            return timedelta(weeks=step)
        return _month_span(t)

    def _floor(self, t: datetime) -> datetime:
        """Округляет время вниз до начала текущей свечи на сетке таймфрейма."""
        if self._unit == "minute":
            return t.replace(
                minute=(t.minute // self._step) * self._step,
                second=0,
                microsecond=0,
            )
        if self._unit == "hour":
            return t.replace(
                hour=(t.hour // self._step) * self._step,
                minute=0,
                second=0,
                microsecond=0,
            )
        if self._unit == "day":
            return t.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._unit == "week":
            monday = t - timedelta(days=t.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
