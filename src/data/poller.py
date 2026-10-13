"""Фоновый приём live-свечей, независимый от расчёта стратегий.

``LiveMarketDataPoller`` — supervisor полос ingestion: unary maintenance
(прогрев, свежий polling, recovery, сверка, календарь) и необязательная
stream-полоса. Торговый поток не выполняет здесь сеть и только читает кэш.
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext

from src.data.request_budget import RequestDeferred
from src.logging_setup import get_logger

log = get_logger(__name__)


class LiveMarketDataPoller:
    """Lifecycle фоновых полос: start/stop, пробуждение и ожидание старта."""

    # Пауза полосы после непредвиденной ошибки шага: без горячего цикла.
    LANE_ERROR_PAUSE = 5.0
    # Холостой ритм полосы полноты: её работа измеряется часами, не секундами.
    COMPLETENESS_IDLE = 5.0

    def __init__(self, cache, timeframes, poll_seconds: float, *, worker=None, stream=None, lanes=(),
                 status_interval: float | None = None) -> None:
        self._cache = cache
        self._timeframes = tuple(sorted(set(timeframes)))
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._worker = worker
        self._stream = stream
        self._lanes = tuple(lanes)
        self._status_interval = status_interval
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._lane_wake = {lane.name: threading.Event() for lane in self._lanes}
        self._threads: list[threading.Thread] = []
        self._background_started = False
        self._live_started = False

    def _budget_lane(self, name):
        budget = getattr(self._cache, "_request_budget", None)
        return budget.lane(name) if budget is not None and hasattr(budget, "lane") else nullcontext()

    def start_background(self) -> None:
        """Полосы доказательств, полноты и календаря — до выбора инструментов."""
        if self._background_started:
            return
        self._background_started = True
        for lane in self._lanes:
            thread = threading.Thread(target=self._run_lane, args=(lane,), name=f"market-data-{lane.name}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def attach_stream(self, stream) -> None:
        """Поток подключается после выбора инструментов, до старта живой полосы."""
        if self._live_started:
            raise RuntimeError("Поток подключается до старта живой полосы")
        self._stream = stream

    def start(self) -> None:
        self.start_background()
        if self._live_started:
            return
        self._live_started = True
        target = self._run_worker if self._worker is not None else self._run
        live = [threading.Thread(target=target, name="live-market-data", daemon=True)]
        if self._stream is not None:
            live.append(threading.Thread(target=self._stream.run, name="live-market-stream", daemon=True))
        self._threads.extend(live)
        for thread in live:
            thread.start()

    def wake(self) -> None:
        self._wake.set()
        for event in self._lane_wake.values():
            event.set()

    def wait_startup(self, timeout=None, *, sleep=time.sleep) -> bool:
        """Ограниченное ожидание первичного прогрева/handoff перед обзором.

        Timeout ограничивает блокировку запуска, а не полноту: неготовые пары
        остаются неготовыми и диагностируются.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stopped.is_set():
            ready = self._worker is None or self._worker.startup_complete()
            if ready and self._stream is not None:
                ready = self._stream.startup_complete()
            if ready:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            sleep(0.1)
        return False

    def close(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._stream is not None:
            self._stream.close()
        for thread in self._threads:
            thread.join(timeout=max(5, self._poll_seconds * 2))
        self._threads = []

    def _run_lane(self, lane) -> None:
        """Цикл одной полосы: шаг за шагом, ошибка не останавливает другие полосы."""
        idle = self.COMPLETENESS_IDLE if lane.name == "completeness" else self._poll_seconds
        event = self._lane_wake[lane.name]
        with self._budget_lane(lane.name):
            while not self._stopped.is_set():
                try:
                    worked = lane.step()
                    pause = idle
                except RequestDeferred:
                    worked, pause = False, self._poll_seconds
                except Exception as exc:
                    log.warning("Полоса %s: шаг не удался: %s", lane.name, exc)
                    worked, pause = False, self.LANE_ERROR_PAUSE
                if not worked:
                    event.wait(pause)
                    event.clear()

    def _log_status(self, last) -> float:
        if not self._status_interval:
            return last
        now = time.monotonic()
        if now - last < self._status_interval:
            return last
        budget = getattr(self._cache, "_request_budget", None)
        snapshot = budget.snapshot() if budget is not None and hasattr(budget, "snapshot") else {}
        completeness = next((lane for lane in self._lanes if lane.name == "completeness"), None)
        holes = sum(getattr(completeness, "last_holes", {}).values()) if completeness is not None else 0
        log.info("Полосы данных: запросы за минуту %s; дыр окна к проверке %d", snapshot, holes)
        return now

    def _run_worker(self) -> None:
        last_status = time.monotonic()
        with self._budget_lane("live"):
            self._run_live_worker(last_status)

    def _run_live_worker(self, last_status) -> None:
        while not self._stopped.is_set():
            last_status = self._log_status(last_status)
            try:
                worked = self._worker.step()
            except Exception as exc:
                log.warning("Фоновое обслуживание live-данных не удалось: %s", exc)
                worked = False
            if not worked:
                calendar_delay = self._worker.next_wakeup_delay()
                timeout = self._poll_seconds if calendar_delay is None else max(0.05, min(self._poll_seconds, calendar_delay))
                self._wake.wait(timeout)
                self._wake.clear()

    def _run(self) -> None:
        while not self._stopped.is_set():
            for timeframe in self._timeframes:
                if self._stopped.is_set():
                    return
                try:
                    self._cache.refresh_if_new_candle(timeframe, force=True)
                except Exception as exc:
                    log.warning("Фоновое обновление свечей %s не удалось: %s", timeframe, exc)
            self._stopped.wait(self._poll_seconds)
