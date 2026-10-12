"""Общий лимит фактических RPC с отдельными очередями у владельца задач."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from src.api.retry import _is_rate_limited, rate_limit_reset_secs, DEFAULT_BASE_DELAY


class RequestDeferred(Exception):
    """Запрос остаётся в очереди до разрешённого API-окна."""


class RequestBudget:
    def __init__(self, now, interval: float, sleep=time.sleep):
        self._now = now
        self._interval = max(0, interval)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = None
        self.retry_after = None
        self._inflight = set()
        self.catalogs = {}

    def call(self, fn, *args, key=None, wait=False, pace=True, **kwargs):
        """Выполняет RPC через общий арбитр.

        ``pace=True`` берёт окно ``data_refresh_min_interval`` (пауза нужна для
        повторных свежих дозагрузок). ``pace=False`` не занимает окно: так идут
        страницы одной длинной загрузки истории, чтобы прогрев не растягивался
        на ``интервал × число страниц``. Сброс лимита брокера и дедупликация
        по ``key`` соблюдаются в обоих режимах.
        """
        while True:
            with self._lock:
                now = self._now()
                if key in self._inflight:
                    raise RequestDeferred("Запрос этого диапазона уже выполняется")
                gates = [value for value in (self.retry_after, self._next_at if pace else None)
                         if value is not None]
                until = max(gates, default=now)
                delay = max(0, (until - now).total_seconds())
                if delay == 0:
                    if pace:
                        self._next_at = now + timedelta(seconds=self._interval)
                    self._inflight.add(key)
                    break
            if not wait:
                raise RequestDeferred("Ожидание общего лимита API")
            self._sleep(delay)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limited(exc):
                with self._lock:
                    self.retry_after = self._now() + timedelta(
                        seconds=max(rate_limit_reset_secs(exc) or DEFAULT_BASE_DELAY, self._interval))
            raise
        finally:
            with self._lock:
                self._inflight.discard(key)
