from __future__ import annotations

from src.logging_setup import get_logger
from src.scheduler.clock import Clock, HistoricalClock, as_clock

log = get_logger(__name__)


def _stamp(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


class RunSession:
    """Условия завершения прогона.

    Боевой режим не завершается никогда: цикл крутится до Ctrl+C. Исторический
    прогон завершается по достижении правой границы диапазона или по концу
    доступных данных, а объект сообщает причину остановки рыночным моментом.
    """

    is_virtual: bool = False

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = as_clock(clock)

    @property
    def clock(self) -> Clock:
        return self._clock

    def should_stop(self) -> bool:
        return False

    def stop_reason(self) -> str:
        return ""

    def mark_data_exhausted(self) -> None:
        """Кэш сообщил об отсутствии новых баров. Боевой режим это игнорирует."""
        return None

    def market_now(self):
        return self._clock.now()


class LiveRunSession(RunSession):
    """Боевой режим: ``should_stop()`` всегда ``False`` — цикл бесконечный."""

    def should_stop(self) -> bool:
        return False


class HistoricalRunSession(RunSession):
    """Исторический прогон: рыночное время виртуальное, у прогона есть конец."""

    is_virtual = True

    def __init__(self, clock: HistoricalClock) -> None:
        super().__init__(clock)
        self._data_exhausted_at = None

    @property
    def start(self):
        return self._clock.start

    @property
    def end(self):
        return self._clock.end

    def mark_data_exhausted(self) -> None:
        """Кэш сообщил, что свежих баров больше нет: это конец прогона."""
        if self._data_exhausted_at is None:
            self._data_exhausted_at = self._clock.now()

    def should_stop(self) -> bool:
        return self._data_exhausted_at is not None or self._clock.finished

    def stop_reason(self) -> str:
        if self._data_exhausted_at is not None:
            return f"данные закончились на {_stamp(self._data_exhausted_at)}"
        if self._clock.finished:
            return (
                f"диапазон [{_stamp(self.start)} — {_stamp(self.end)}) обработан полностью"
            )
        return ""

    def ticks_done(self) -> int:
        """Сколько тиков рыночного времени уже отработано."""
        if self._clock.now() <= self._clock.start:
            return 0
        return int(
            (self._clock.now() - self._clock.start).total_seconds()
            // self._clock.step.total_seconds()
        )


def build_run_session(mode: str, clock=None) -> RunSession:
    """Объект прогона для режима запуска: ``live`` или ``history``."""
    if mode == "live":
        return LiveRunSession(clock)
    if mode == "history":
        if not isinstance(clock, HistoricalClock):
            raise ValueError("Историческому прогону нужны виртуальные часы HistoricalClock")
        return HistoricalRunSession(clock)
    raise ValueError(f"Неизвестный режим запуска: {mode!r}")
