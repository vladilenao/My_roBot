"""Фоновый приём live-свечей, независимый от расчёта стратегий."""

from __future__ import annotations

import threading

from src.logging_setup import get_logger

log = get_logger(__name__)


class LiveMarketDataPoller:
    """Периодически обновляет кэш и локальный журнал в отдельном потоке."""

    def __init__(self, cache, timeframes, poll_seconds: float) -> None:
        self._cache = cache
        self._timeframes = tuple(sorted(set(timeframes)))
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="live-market-data", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5, self._poll_seconds * 2))
            self._thread = None

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
