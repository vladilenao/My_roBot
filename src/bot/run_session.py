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

    def mark_data_exhausted(self, exhaustion=None) -> None:
        """Кэш сообщил об отсутствии новых баров. Боевой режим это игнорирует."""
        return None

    def mark_data_gap(self, gap) -> None:
        """Кэш нашёл разрыв в данных и знает момент следующего бара.

        Боевой режим игнорирует: там пауза в данных не должна двигать рыночное
        время, с которым работает боевая стратегия.
        """
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
        self._exhaustion = None
        self._gaps_skipped = 0

    @property
    def start(self):
        return self._clock.start

    @property
    def end(self):
        return self._clock.end

    @property
    def covered(self) -> bool:
        """Дошёл ли прогон до правой границы диапазона (полный охват)."""
        return self._data_exhausted_at is None and self._clock.finished

    @property
    def exhaustion(self):
        """Границы проверенного горизонта поиска при остановке из-за данных (или None)."""
        return self._exhaustion

    @property
    def gaps_jumped(self) -> int:
        """Сколько разрывов данных прогон перепрыгнул (не все разрывы требуют прыжка)."""
        return self._gaps_skipped

    def mark_data_exhausted(self, exhaustion=None) -> None:
        """Кэш сообщил, что свежих баров больше нет: это конец прогона.

        ``exhaustion`` несёт границы проверенного горизонта поиска: без них
        остановку нельзя отличить «данных нет до конца диапазона» от «поиск
        оборван семью сутками раньше конца».
        """
        if self._data_exhausted_at is None:
            self._data_exhausted_at = self._clock.now()
            self._exhaustion = exhaustion

    def mark_data_gap(self, gap) -> None:
        """Переводит рыночное время на момент следующего имеющегося бара.

        Разрыв в данных — не конец прогона: за него «сгорело» несколько тиков без
        прогресса, но данные дальше есть. Часы прыгают к следующему бару, чтобы
        прогон не принял паузу за конец доступных данных.
        """
        self._gaps_skipped += 1
        self._clock.jump_to(gap.resume)
        log.info(
            "Разрыв данных с %s до %s (пропущено баров: %d): рыночное время переведено на %s.",
            gap.since, gap.resume, gap.missed, self._clock.now(),
        )

    def should_stop(self) -> bool:
        return self._data_exhausted_at is not None or self._clock.finished

    def stop_reason(self) -> str:
        if self._data_exhausted_at is not None:
            stop_at = _stamp(self._data_exhausted_at)
            exhaustion = self._exhaustion
            if exhaustion is None:
                return (
                    f"данные закончились на {stop_at}, "
                    f"до конца диапазона ({_stamp(self.end)}) прогон не дошёл"
                )
            horizon = f"{_stamp(exhaustion.horizon_start)} — {_stamp(exhaustion.horizon_end)}"
            if exhaustion.horizon_limited:
                # Поиск оборван семью сутками раньше конца диапазона: утверждать,
                # что данных за пределами проверенного участка нет, нельзя.
                return (
                    f"следующий бар не найден в проверенном горизонте [{horizon}], "
                    f"после него данные не проверялись и их отсутствие не утверждается; "
                    f"диапазон обработан не полностью, до конца ({_stamp(self.end)}) не дойдено"
                )
            return (
                f"данные закончились на {stop_at}; проверен весь остаток диапазона "
                f"[{horizon}], до конца диапазона ({_stamp(self.end)}) прогон не дошёл"
            )
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
