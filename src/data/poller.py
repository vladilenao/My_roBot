"""Фоновый приём live-свечей, независимый от расчёта стратегий.

``LiveMarketDataPoller`` — supervisor полос ingestion: unary maintenance
(прогрев, свежий polling, recovery, сверка, календарь) и необязательная
stream-полоса. Торговый поток не выполняет здесь сеть и только читает кэш.
"""

from __future__ import annotations

import threading
import time

from src.logging_setup import get_logger

log = get_logger(__name__)


class LiveMarketDataPoller:
    """Lifecycle фоновых полос: start/stop, пробуждение и ожидание старта."""

    def __init__(self, cache, timeframes, poll_seconds: float, *, worker=None, stream=None) -> None:
        self._cache = cache
        self._timeframes = tuple(sorted(set(timeframes)))
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._worker = worker
        self._stream = stream
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        target = self._run_worker if self._worker is not None else self._run
        self._threads.append(threading.Thread(target=target, name="live-market-data", daemon=True))
        if self._stream is not None:
            self._threads.append(threading.Thread(target=self._stream.run, name="live-market-stream", daemon=True))
        for thread in self._threads:
            thread.start()

    def wake(self) -> None:
        self._wake.set()

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

    def _run_worker(self) -> None:
        while not self._stopped.is_set():
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
