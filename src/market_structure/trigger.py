"""Постановка закрытых баров воркеру паттернов на стороне ядра (add-contour-market-feed).

Раньше бар ставил торговый тик контура. Теперь ядро само следит за парами,
которые контур передал в потребностях (ТФ привязок выбранных инструментов):
отдельный поток просыпается на изменении данных кэша ядра, ставит стартовую
отметку, когда пара впервые прогрета, и затем — каждый новый закрытый бар.
Повторы одного бара отсекает сам воркер.
"""

from __future__ import annotations

import threading

from src.logging_setup import get_logger

log = get_logger(__name__)


class PatternTrigger:
    """Сторож последнего закрытого бара пар потребности контура."""

    def __init__(self, cache, timeline, worker, pairs, *, wait_secs: float = 1.0) -> None:
        self._cache = cache
        self._timeline = timeline
        self._worker = worker
        self._pairs = list(pairs)
        self._wait_secs = wait_secs
        self._sent: dict[tuple[str, str], object] = {}
        # Пары, о пропуске которых без UID уже предупредили (до появления UID).
        self._uid_missing: set[tuple[str, str]] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pattern-trigger", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        generation = None
        while not self._stop.is_set():
            try:
                self.observe()
            except Exception:
                log.exception("Паттерны: ошибка постановки закрытых баров")
            generation = self._cache.wait_for_data_change(generation, self._wait_secs)

    def observe(self) -> None:
        """Один проход по парам: стартовая отметка либо новый закрытый бар."""
        for instrument, timeframe in self._pairs:
            uid = self._cache.pair_uid(instrument, timeframe)
            missing = (instrument.ticker, instrument.instrument_type, timeframe)
            if not uid:
                if missing not in self._uid_missing:
                    self._uid_missing.add(missing)
                    log.warning("Паттерны: UID пары %s %s неизвестен — закрытые бары не анализируются",
                                instrument.short_name or "контракт не указан", timeframe)
                continue
            self._uid_missing.discard(missing)
            last, ready = self._cache.pattern_bar(instrument, timeframe)
            if last is None or not ready:
                continue
            close = self._timeline.grid(timeframe).bar_close(last)
            pair = uid, timeframe
            if pair not in self._sent:
                self._worker.prime(instrument_uid=uid, timeframe=timeframe, close_time=close)
            elif close > self._sent[pair]:
                self._worker.submit(ticker=instrument.ticker, instrument_uid=uid, timeframe=timeframe,
                                    close_time=close)
            else:
                continue
            self._sent[pair] = close
