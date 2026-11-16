"""Фоновый приём live-свечей, независимый от расчёта стратегий.

``LiveMarketDataPoller`` — supervisor полос ingestion: unary maintenance
(прогрев, свежий polling, recovery, сверка, календарь) и необязательная
stream-полоса. Торговый поток не выполняет здесь сеть и только читает кэш.
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext

from src.data.lane_watchdog import LaneWatchdog
from src.data.request_budget import RequestDeferred
from src.logging_setup import get_logger

log = get_logger(__name__)


class LiveMarketDataPoller:
    """Lifecycle фоновых полос: start/stop, пробуждение и ожидание старта."""

    # Пауза полосы после непредвиденной ошибки шага: без горячего цикла.
    LANE_ERROR_PAUSE = 5.0
    # Холостой ритм полосы полноты: её работа измеряется часами, не секундами.
    COMPLETENESS_IDLE = 5.0
    # Имя живой полосы у сторожа и в ``GET /api/v1/core``.
    LIVE_LANE = "live"
    # Наибольший период проверки сторожа (fix-live-lane-stall, design 2).
    WATCHDOG_PERIOD = 30.0

    def __init__(self, cache, timeframes, poll_seconds: float, *, worker=None, stream=None, lanes=(),
                 status_interval: float | None = None, stall_timeout: float = 300.0, watchdog=None) -> None:
        self._cache = cache
        self._timeframes = tuple(sorted(set(timeframes)))
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._worker = worker
        self._stream = stream
        self._lanes = tuple(lanes)
        self._status_interval = status_interval
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._watchdog_wake = threading.Event()
        self._lane_wake = {lane.name: threading.Event() for lane in self._lanes}
        self.watchdog = watchdog if watchdog is not None else LaneWatchdog(stall_timeout)
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
        # Сводку и проверку зависания ведёт свой поток: зависшая полоса не уносит их с собой.
        watchdog = threading.Thread(target=self._run_watchdog, name="market-data-watchdog", daemon=True)
        self._threads.append(watchdog)
        watchdog.start()
        for lane in self._lanes:
            self.watchdog.register(lane.name)
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
        self.watchdog.register(self.LIVE_LANE)
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

    def retire_unrequested_pairs(self):
        """Снимает пары, не запрошенные запуском; в составе они станут прошлыми.

        Возвращает снятые пары как (тикер, ТФ).
        """
        retired = self._cache.retire_unrequested_live_pairs()
        if retired and self._worker is not None:
            self._worker.retire_roster_pairs([(uid, timeframe) for uid, timeframe, _ in retired])
            self.wake()
        return [(ticker, timeframe) for _, timeframe, ticker in retired]

    def startup_pending(self) -> list[str]:
        """Пары, которых ждёт ``wait_startup``: «ТИКЕР ТФ: причина»."""
        pending = {}
        if self._worker is not None:
            pending.update(self._worker.startup_pending())
        if self._stream is not None:
            pending.update(self._stream.startup_pending())   # поток уточняет причину
        lines = []
        for (uid, timeframe), reason in pending.items():
            pair = self._cache.live_pair(uid, timeframe)
            name = pair["instrument"].ticker if pair is not None else uid
            lines.append(f"{name} {timeframe}: {reason}")
        return sorted(lines)

    def close(self) -> None:
        self._stopped.set()
        self._wake.set()
        self._watchdog_wake.set()
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
                self.watchdog.begin(lane.name)
                try:
                    worked = lane.step()
                    pause = idle
                except RequestDeferred:
                    worked, pause = False, self._poll_seconds
                except Exception as exc:
                    log.warning("Полоса %s: шаг не удался: %s", lane.name, exc)
                    worked, pause = False, self.LANE_ERROR_PAUSE
                finally:
                    self.watchdog.end(lane.name)
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
        lanes = {getattr(lane, "name", ""): lane for lane in self._lanes}
        holes = sum(getattr(lanes.get("completeness"), "last_holes", {}).values())
        waiting = len(getattr(lanes.get("evidence"), "_next", {}))
        log.info("Полосы данных: запросы за минуту %s; дыр окна к проверке %d; пар ждут доказательства %d%s",
                 snapshot, holes, waiting, self.watchdog.stalled_text())
        return now

    def _run_watchdog(self) -> None:
        """Поток сторожа: зависшие шаги и периодическая сводка полос (fix-live-lane-stall)."""
        period = max(0.05, min(self.WATCHDOG_PERIOD, self.watchdog.threshold / 4))
        last_status = time.monotonic()
        while not self._stopped.is_set():
            try:
                self.watchdog.check()
                last_status = self._log_status(last_status)
            except Exception as exc:
                log.warning("Сторож полос данных: проверка не удалась: %s", exc)
            self._watchdog_wake.wait(period)

    def _run_worker(self) -> None:
        with self._budget_lane(self.LIVE_LANE):
            self._run_live_worker()

    def _run_live_worker(self) -> None:
        while not self._stopped.is_set():
            self.watchdog.begin(self.LIVE_LANE)
            try:
                worked = self._worker.step()
            except Exception as exc:
                log.warning("Фоновое обслуживание live-данных не удалось: %s", exc)
                worked = False
            finally:
                self.watchdog.end(self.LIVE_LANE)
            if not worked:
                calendar_delay = self._worker.next_wakeup_delay()
                timeout = self._poll_seconds if calendar_delay is None else max(0.05, min(self._poll_seconds, calendar_delay))
                self._wake.wait(timeout)
                self._wake.clear()

    def _run(self) -> None:
        while not self._stopped.is_set():
            self.watchdog.begin(self.LIVE_LANE)
            try:
                for timeframe in self._timeframes:
                    if self._stopped.is_set():
                        return
                    try:
                        self._cache.refresh_if_new_candle(timeframe, force=True)
                    except Exception as exc:
                        log.warning("Фоновое обновление свечей %s не удалось: %s", timeframe, exc)
            finally:
                self.watchdog.end(self.LIVE_LANE)
            self._stopped.wait(self._poll_seconds)
