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


class StoppableRunSession(LiveRunSession):
    """Процесс контура (add-contour-process): цикл до флага остановки, проверяемого на границе тика."""

    def __init__(self, stop_event, clock=None) -> None:
        super().__init__(clock)
        self._stop_event = stop_event

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def stop_reason(self) -> str:
        return "остановка контура" if self._stop_event.is_set() else ""


class HistoricalRunSession(RunSession):
    """Исторический прогон: рыночное время виртуальное, у прогона есть конец."""

    is_virtual = True

    def __init__(self, clock: HistoricalClock) -> None:
        super().__init__(clock)
        self._data_exhausted_at = None
        self._exhaustion = None
        self._gaps_skipped = 0
        self._contract_failure = ""
        self.boundary_checks = ()
        self.terminal_gap = None
        self._source_failed = False
        self._drivers_complete = True
        # Стоп прогона командой или остановкой робота (add-history-contour): выход после тика с отчётом.
        self._operator_stop = ""
        # Сигналы и недопуски по привязкам — для отчёта прогона.
        from src.history.admission_stats import SignalAdmissionStats
        self.admission_stats = SignalAdmissionStats()

    def update_data_status(self, *, source_failed, drivers_complete):
        self._source_failed = source_failed
        self._drivers_complete = drivers_complete

    def set_boundary_checks(self, checks):
        self.boundary_checks = tuple(checks)

    def complete_terminal_gap(self, gap):
        if self._data_exhausted_at is not None or gap.end != self.end:
            return False
        # Cache проверяет источник/снимок и фактические обработанные хвосты;
        # сессия принимает только собственное доказательство предпроверки.
        if not gap.checks or any(not item.ok or item.role != "end" or item.boundary != self.end or
            not any(item.identity == own.identity and item == own for own in self.boundary_checks) for item in gap.checks):
            return False
        self.terminal_gap = gap
        self._clock.jump_to(self.end)
        return True

    def mark_contract_failure(self, reason):
        self._contract_failure = reason

    def request_stop(self, reason: str = "остановлено оператором") -> None:
        """Остановить прогон на границе тика; отчёт пишется с этой причиной."""
        if not self._operator_stop:
            self._operator_stop = reason

    @property
    def start(self):
        return self._clock.start

    @property
    def end(self):
        return self._clock.end

    @property
    def covered(self) -> bool:
        """Дошёл ли прогон до правой границы диапазона (полный охват)."""
        return (not self._contract_failure and not self._source_failed and self._drivers_complete
                and self._data_exhausted_at is None and self._clock.finished)

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
        return (bool(self._operator_stop) or bool(self._contract_failure) or self._data_exhausted_at is not None
                or self._clock.finished)

    def stop_reason(self) -> str:
        if self._operator_stop:
            return self._operator_stop
        if self._contract_failure:
            return f"незавершённый контрактный переход: {self._contract_failure}"
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
            if self._source_failed or not self._drivers_complete:
                return "конец диапазона достигнут, но обязательный проверенный хвост не обработан или источник ответил ошибкой; неполный охват"
            if self.terminal_gap is not None:
                return "конец диапазона внутри подтверждённого перерыва"
            return (
                f"диапазон [{_stamp(self.start)} — {_stamp(self.end)}) обработан полностью"
            )
        return ""

    def ticks_done(self) -> int:
        """Сколько тиков рыночного времени уже отработано."""
        if self._clock.now() <= self._clock.start:
            return 0
        processed_until = self._clock.now() if self.terminal_gap is None else self.terminal_gap.last_processed_bar+self._clock.step
        return int(
            (processed_until - self._clock.start).total_seconds()
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
