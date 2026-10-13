"""Общий лимит фактических RPC с отдельными очередями у владельца задач."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import timedelta

from src.api.retry import _is_rate_limited, rate_limit_reset_secs, DEFAULT_BASE_DELAY


class RequestDeferred(Exception):
    """Запрос остаётся в очереди до разрешённого API-окна."""


class RequestBudget:
    def __init__(self, now, interval: float, sleep=time.sleep, per_minute=None, per_minute_by_service=None):
        self._now = now
        self._interval = max(0, interval)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = None
        self.retry_after = None
        self._inflight = set()
        self.catalogs = {}
        # Локальный потолок фактических unary RPC в минуту: каждая страница,
        # включая pace=False, расходует окно (design 8, задача 5.4).
        if per_minute is not None and (isinstance(per_minute, bool) or not isinstance(per_minute, int) or per_minute <= 0):
            raise ValueError("per_minute должен быть положительным целым")
        self._per_minute = per_minute
        self._sent = deque()
        limits = per_minute_by_service or {}
        if not isinstance(limits, dict) or any(
            not isinstance(name, str) or not name or isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            for name, limit in limits.items()
        ):
            raise ValueError("per_minute_by_service должен содержать положительные целые лимиты")
        self._per_minute_by_service = dict(limits)
        self._sent_by_service = {name: deque() for name in limits}
        self.calls = 0

    def call(self, fn, *args, key=None, wait=False, pace=True, service=None, **kwargs):
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
                window = now - timedelta(minutes=1)
                while self._sent and self._sent[0] <= window:
                    self._sent.popleft()
                sent_for_service = self._sent_by_service.get(service)
                if sent_for_service is not None:
                    while sent_for_service and sent_for_service[0] <= window:
                        sent_for_service.popleft()
                quota = (self._sent[0] + timedelta(minutes=1)
                          if self._per_minute is not None and len(self._sent) >= self._per_minute else None)
                service_quota = (
                    sent_for_service[0] + timedelta(minutes=1)
                    if sent_for_service is not None and len(sent_for_service) >= self._per_minute_by_service[service]
                    else None
                )
                gates = [value for value in (self.retry_after, self._next_at if pace else None, quota, service_quota)
                         if value is not None]
                until = max(gates, default=now)
                delay = max(0, (until - now).total_seconds())
                if delay == 0:
                    if pace:
                        self._next_at = now + timedelta(seconds=self._interval)
                    self._sent.append(now)
                    if sent_for_service is not None:
                        sent_for_service.append(now)
                    self.calls += 1
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
