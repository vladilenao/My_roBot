"""Чтение Redis Stream ``cluster.events`` в локальное хранилище.

Live: своя Consumer Group, сначала собственные неподтверждённые записи, затем
новые; ``XACK`` только после фиксации пачки в SQLite. История: разовая
догрузка ``XRANGE`` без группы и подтверждений.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

from src.clusters.store import ClusterEventStore, utc_text
from src.logging_setup import get_logger

log = get_logger(__name__)
CONSUMER_NAME = "robot"
BATCH = 500
BLOCK_MS = 5000
MAX_PAUSE = 60.0


def _entries(response) -> list[tuple[str, dict[str, str]]]:
    """Ответ XREADGROUP/XRANGE → список ``(stream_id, поля)``."""
    if not response:
        return []
    if isinstance(response, dict):  # RESP3
        response = list(response.items())
    if response and isinstance(response[0], (list, tuple)) and len(response[0]) == 2 and isinstance(response[0][1], list):
        flat = []
        for _, items in response:
            flat.extend(items)
        response = flat
    return [(str(stream_id), {str(k): str(v) for k, v in (fields or {}).items()}) for stream_id, fields in response]


class ClusterEventsConsumer:
    """Фоновый потребитель; ``start`` не блокирует торговый цикл."""

    def __init__(self, store: ClusterEventStore, redis_client, *, stream: str, group: str,
                 consumer: str = CONSUMER_NAME, clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._store = store
        self._redis = redis_client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_read_at: datetime | None = None
        self._group_ready = False
        self._pending_done = False

    @property
    def last_read_at(self) -> datetime | None:
        """Момент последнего успешного чтения Redis (``None`` — ещё не было)."""
        return self._last_read_at

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cluster-events", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        pause = 1.0
        while not self._stop.is_set():
            try:
                self.step()
                pause = 1.0
            except Exception as exc:  # Redis/SQLite: торговля не останавливается
                log.warning("Кластеры: чтение %s не удалось (%s), повтор через %.0f с",
                            self._stream, type(exc).__name__, pause)
                self._stop.wait(pause)
                pause = min(pause * 2, MAX_PAUSE)

    def step(self) -> int:
        """Одна итерация: создать группу, прочитать пачку, записать, подтвердить."""
        if not self._group_ready:
            self._ensure_group()
        start = ">" if self._pending_done else "0"
        response = self._redis.xreadgroup(
            self._group, self._consumer, {self._stream: start}, count=BATCH,
            block=None if start == "0" else BLOCK_MS,
        )
        entries = _entries(response)
        now = self._clock()
        self._last_read_at = now
        if start == "0" and not entries:
            self._pending_done = True
        if not entries:
            self._store.set_state("last_read_at", utc_text(now))
            return 0
        inserted = self._store.write(entries, received_at=now)
        self._redis.xack(self._stream, self._group, *[stream_id for stream_id, _ in entries])
        self._store.set_state("last_read_at", utc_text(now))
        return inserted

    def _ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            log.info("Кластеры: создана группа %s потока %s", self._group, self._stream)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True


def catch_up(store: ClusterEventStore, redis_client, *, stream: str) -> int:
    """Догрузка потока перед историческим прогоном; Redis недоступен — 0 и предупреждение."""
    start = store.state("last_stream_id")
    cursor = f"({start}" if start else "-"
    total = 0
    try:
        while True:
            entries = _entries(redis_client.xrange(stream, min=cursor, max="+", count=BATCH))
            if not entries:
                break
            total += store.write(entries)
            cursor = f"({entries[-1][0]}"
    except Exception as exc:
        log.warning("Кластеры: догрузка %s не удалась (%s), прогон идёт по сохранённым событиям",
                    stream, type(exc).__name__)
    else:
        log.info("Кластеры: догружено %s событий потока %s", total, stream)
    return total
