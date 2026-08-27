from __future__ import annotations

import logging
import time

from src.instruments import Instrument, normalize_instrument
from src.strategies.registry import get_strategy, validate_assignments

log = logging.getLogger(__name__)


class TradingBot:
    """Оркестратор-сценарий: каждый этап работы робота — отдельный метод.

    Ритм — «один тик = одна закрытая свеча»: итерация выравнивается по границе
    закрытия свечи таймфрейма. Решения принимаются только по готовым (закрытым)
    свечам, а уведомление выполняется об изменении сигнала (фронт), а не о каждом
    проходе.
    """

    def __init__(
        self,
        instruments,
        notifier,
        strategy_map: dict[str, object],
        data_cache,
        timeline,
        execution,
        strategy_factory=get_strategy,
        share_strategies: dict[str, list[str]] | None = None,
        future_strategies: dict[str, list[str]] | None = None,
        heartbeat_every_ticks: int | None = 60,
    ) -> None:
        self._notifier = notifier
        self._strategy_map = strategy_map
        self._data_cache = data_cache
        self._timeline = timeline
        self._execution = execution
        self._strategy_factory = strategy_factory
        self._share_strategies = share_strategies or {}
        self._future_strategies = future_strategies or {}
        self._heartbeat_every = heartbeat_every_ticks or 0

        self._instruments = [
            i if isinstance(i, Instrument) else normalize_instrument(i)
            for i in instruments
        ]
        self._prev_state: dict[tuple, tuple] = {}
        self._tick_count = 0
        self._errors_in_period = 0
        self._heartbeat_countdown = self._heartbeat_every

    # ── ПУНКТ 1: запуск ──
    def run(self) -> None:
        self._validate()
        log.info("Робот запущен. Ctrl+C для остановки.")
        self._loop()

    # ── ПУНКТ 2: бесконечный цикл «тик = закрытая свеча» ──
    def _loop(self) -> None:
        while True:
            try:
                self._timeline.wait_until_candle_close()
                self._tick()
            except KeyboardInterrupt:
                log.info("Бот остановлен.")
                return
            except Exception as exc:
                self._report_error(exc)
                time.sleep(self._timeline.fallback_secs())

    # ── ПУНКТ 3: один тик — обновить данные и обработать инструменты ──
    def _tick(self) -> None:
        self._data_cache.refresh_if_new_candle()
        for instrument in self._instruments:
            self._process(instrument)
        self._maybe_heartbeat()

    # ── ПУНКТ 4: по инструменту ──
    def _process(self, instrument: Instrument) -> None:
        strategies = self._strategies_for(instrument)
        if not strategies:
            log.info("Для %s не назначено стратегий — пропускаем.", instrument.label)
            return
        frame = self._data_cache.frame_for(instrument)
        if frame.empty:
            log.info("Нет готовых свечей для %s — пропускаем.", instrument.label)
            return
        self._analyze(instrument, strategies, frame)

    # ── ПУНКТ 4.2: анализ по каждой стратегии ──
    def _analyze(self, instrument: Instrument, strategies: list[str], frame) -> None:
        for name in strategies:
            try:
                strategy = self._strategy_factory(name, config=self._strategy_map[name])
                ta = strategy.compute(frame)
                decision = strategy.decide(ta, timeframe=self._timeline.timeframe)
                self._emit(instrument, name, decision)
            except Exception:
                log.exception(
                    "Ошибка стратегии '%s' на %s — идём дальше.", name, instrument.label
                )

    # ── ПУНКТ 4.2.3-4.2.4: фронт сигнала + доставка через порт ──
    def _emit(self, instrument: Instrument, name: str, decision) -> None:
        key = (instrument.ticker, name)
        state = (decision.signal_type.value, round(decision.price, 4))
        if state == self._prev_state.get(key):
            log.debug("Сигнал %s по %s не изменился — пропуск.", name, instrument.label)
            return
        self._prev_state[key] = state
        self._execution.execute(decision, instrument)

    # ── диспетчеризация стратегий по типу инструмента ──
    def _strategies_for(self, instrument: Instrument) -> list[str]:
        if instrument.instrument_type == "future":
            return self._future_strategies.get(instrument.ticker[:2].upper(), [])
        return self._share_strategies.get(instrument.ticker, [])

    # ── валидация привязок до цикла (fail-fast) ──
    def _validate(self) -> None:
        validate_assignments(self._share_strategies, source="SHARE_STRATEGIES")
        validate_assignments(self._future_strategies, source="FUTURE_STRATEGIES")

    # ── диагностика ──
    def _maybe_heartbeat(self) -> None:
        if self._heartbeat_every <= 0:
            return
        self._tick_count += 1
        self._heartbeat_countdown -= 1
        if self._heartbeat_countdown > 0:
            return
        self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        message = (
            f"💓 Сердцебиение: тиков работы — {self._tick_count}, "
            f"ошибок за период — {self._errors_in_period}."
        )
        try:
            self._notifier.notify(message)
        except Exception:
            log.exception("Не удалось доставить сердцебиение.")
        self._errors_in_period = 0
        self._heartbeat_countdown = self._heartbeat_every

    def _report_error(self, exc: Exception) -> None:
        self._errors_in_period += 1
        log.exception("Ошибка тика: %s", exc)
        try:
            self._notifier.notify(f"❗ Ошибка робота: {exc}")
        except Exception:
            log.exception("Не удалось уведомить об ошибке робота.")
