"""Арбитр лимитов брокера: сервисы раздельно, у полос — гарантированные доли.

Через ``RequestBudget.call`` проходят все unary RPC процесса. Сервисы
источника (котировки и инструменты) имеют отдельные минутные окна, потолки
и паузы после ``RESOURCE_EXHAUSTED``: ограничение котировок не
останавливает календарь (design 2 add-independent-market-data-lanes).

Полоса берётся из аргумента ``lane`` или из контекста потока
(``budget.lane("evidence")``), поэтому загрузчик и кэш её не знают. Полоса
гарантированно получает свою долю потолка сервиса; незанятую долю других
она может занять, если более приоритетная полоса не ждёт квоты.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import timedelta

from src.api.retry import _is_rate_limited, rate_limit_reset_secs, DEFAULT_BASE_DELAY


class RequestDeferred(Exception):
    """Запрос остаётся в очереди до разрешённого API-окна."""


SERVICE_QUOTES = "quotes"
SERVICE_INSTRUMENTS = "instruments"
_SERVICE_ALIASES = {None: SERVICE_QUOTES, "market_data": SERVICE_QUOTES, SERVICE_QUOTES: SERVICE_QUOTES,
                    SERVICE_INSTRUMENTS: SERVICE_INSTRUMENTS}

# Порядок при конкуренции: интерактивный выбор пользователя, затем
# оперативные свечи, доказательства, полнота, календарь.
LANE_ORDER = ("interactive", "live", "evidence", "completeness", "calendar")
DEFAULT_LANE = "live"
# Полоса, недавно получившая отказ, считается ждущей квоты.
_DEMAND_WINDOW = timedelta(seconds=2)

_current_lane: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_lane", default=None)


def service_name(service) -> str:
    return _SERVICE_ALIASES.get(service, str(service))


def _validate_limits(limits, name):
    if not isinstance(limits, dict) or any(
        not isinstance(key, str) or not key or isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for key, value in limits.items()
    ):
        raise ValueError(f"{name} должен содержать положительные целые лимиты")


def _validate_shares(shares):
    if not isinstance(shares, dict):
        raise ValueError("lane_shares должен быть отображением сервис → доли полос")
    for service, lanes in shares.items():
        if not isinstance(lanes, dict) or any(
            not isinstance(lane, str) or isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100
            for lane, pct in lanes.items()
        ):
            raise ValueError(f"Доли полос сервиса {service!r} должны быть процентами 0..100")
        if sum(lanes.values()) > 100:
            raise ValueError(f"Сумма долей полос сервиса {service!r} больше 100")


class RequestBudget:
    def __init__(self, now, interval: float, sleep=time.sleep, per_minute=None, per_minute_by_service=None,
                 lane_shares=None):
        self._now = now
        self._interval = max(0, interval)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = None
        self._retry_after: dict[str, object] = {}
        self._inflight = set()
        self.catalogs = {}
        # Потолок фактических unary RPC сервиса котировок в минуту: каждая
        # страница, включая pace=False, расходует окно.
        if per_minute is not None and (isinstance(per_minute, bool) or not isinstance(per_minute, int) or per_minute <= 0):
            raise ValueError("per_minute должен быть положительным целым")
        limits = dict(per_minute_by_service or {})
        _validate_limits(limits, "per_minute_by_service")
        self._ceiling = {service_name(name): limit for name, limit in limits.items()}
        if per_minute is not None:
            self._ceiling.setdefault(SERVICE_QUOTES, per_minute)
        shares = lane_shares or {}
        _validate_shares(shares)
        self._shares = {service_name(name): dict(lanes) for name, lanes in shares.items()}
        self._sent: dict[str, deque] = {}
        self._demand: dict[tuple[str, str], object] = {}
        self.calls = 0
        self.calls_by_lane: dict[tuple[str, str], int] = {}

    # ── полоса потока ──
    @contextmanager
    def lane(self, name: str):
        """Все вызовы внутри блока (и в этом потоке) относятся к полосе ``name``."""
        token = _current_lane.set(name)
        try:
            yield self
        finally:
            _current_lane.reset(token)

    @staticmethod
    def current_lane() -> str:
        return _current_lane.get() or DEFAULT_LANE

    # ── совместимость: пауза сервиса котировок ──
    @property
    def retry_after(self):
        return self._retry_after.get(SERVICE_QUOTES)

    @retry_after.setter
    def retry_after(self, value):
        self._retry_after[SERVICE_QUOTES] = value

    def retry_after_for(self, service) -> object:
        return self._retry_after.get(service_name(service))

    def snapshot(self) -> dict:
        """Запросы за последнюю минуту по сервисам и полосам — для диагностики."""
        with self._lock:
            now = self._now()
            result = {}
            for service, sent in self._sent.items():
                self._prune(sent, now)
                lanes = {}
                for _, lane in sent:
                    lanes[lane] = lanes.get(lane, 0) + 1
                result[service] = {"per_minute": len(sent), "ceiling": self._ceiling.get(service),
                                   "lanes": lanes, "retry_after": self._retry_after.get(service)}
            return result

    # ── планирование ──
    @staticmethod
    def _prune(sent, now):
        window = now - timedelta(minutes=1)
        while sent and sent[0][0] <= window:
            sent.popleft()

    def _priority(self, lane):
        return LANE_ORDER.index(lane) if lane in LANE_ORDER else len(LANE_ORDER)

    def _guaranteed(self, service, lane, ceiling):
        pct = self._shares.get(service, {}).get(lane, 0)
        return int(ceiling * pct / 100)

    def _quota_gate(self, service, lane, now):
        """Момент, когда полоса может отправить запрос, либо None — можно сейчас."""
        ceiling = self._ceiling.get(service)
        sent = self._sent.setdefault(service, deque())
        self._prune(sent, now)
        if ceiling is None:
            return None
        if len(sent) >= ceiling:
            return sent[0][0] + timedelta(minutes=1)
        used = {}
        for _, item in sent:
            used[item] = used.get(item, 0) + 1
        if used.get(lane, 0) < self._guaranteed(service, lane, ceiling):
            return None
        # Заимствование: свободное место за вычетом неиспользованных гарантий
        # более приоритетных полос, которые сейчас ждут квоты.
        reserved = 0
        for other in LANE_ORDER[:self._priority(lane)]:
            waiting = self._demand.get((service, other))
            if waiting is not None and now - waiting <= _DEMAND_WINDOW:
                reserved += max(0, self._guaranteed(service, other, ceiling) - used.get(other, 0))
                if self._guaranteed(service, other, ceiling) == 0:
                    reserved += 1
        if ceiling - len(sent) - reserved > 0:
            return None
        oldest = next((stamp for stamp, item in sent if item == lane), sent[0][0])
        return oldest + timedelta(minutes=1)

    def call(self, fn, *args, key=None, wait=False, pace=True, service=None, lane=None, **kwargs):
        """Выполняет RPC через общий арбитр.

        ``pace=True`` берёт окно ``interval`` (прежнее окно свежих
        дозагрузок; в режиме независимых полос интервал равен 0).
        ``pace=False`` окно не занимает. Пауза сервиса после ограничения,
        потолок, доли полос и дедупликация по ``key`` соблюдаются всегда.
        """
        service = service_name(service)
        lane = lane or self.current_lane()
        while True:
            with self._lock:
                now = self._now()
                if key is not None and key in self._inflight:
                    raise RequestDeferred("Запрос этого диапазона уже выполняется")
                gates = [value for value in (self._retry_after.get(service), self._next_at if pace else None,
                                             self._quota_gate(service, lane, now)) if value is not None]
                until = max(gates, default=now)
                delay = max(0, (until - now).total_seconds())
                if delay == 0:
                    if pace and self._interval:
                        self._next_at = now + timedelta(seconds=self._interval)
                    self._sent.setdefault(service, deque()).append((now, lane))
                    self._demand.pop((service, lane), None)
                    self.calls += 1
                    self.calls_by_lane[(service, lane)] = self.calls_by_lane.get((service, lane), 0) + 1
                    if key is not None:
                        self._inflight.add(key)
                    break
                self._demand[(service, lane)] = now
            if not wait:
                raise RequestDeferred("Ожидание лимита API")
            self._sleep(delay)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limited(exc):
                with self._lock:
                    self._retry_after[service] = self._now() + timedelta(
                        seconds=max(rate_limit_reset_secs(exc) or DEFAULT_BASE_DELAY, self._interval))
            raise
        finally:
            if key is not None:
                with self._lock:
                    self._inflight.discard(key)

    def wrap_client(self, client, *, lane: str):
        """Клиент, чьи вызовы сервисов идут через арбитр с ожиданием квоты.

        Для кода, который обращается к ``client.instruments`` и
        ``client.market_data`` напрямую (выбор инструментов, контракты).
        """
        return _ArbitratedClient(client, self, lane)


class _ArbitratedService:
    def __init__(self, inner, budget, service, lane):
        self._inner, self._budget, self._service, self._lane = inner, budget, service, lane

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            return self._budget.call(attr, *args, wait=True, pace=False, service=self._service,
                                     lane=self._lane, **kwargs)
        return call


class _ArbitratedClient:
    def __init__(self, inner, budget, lane):
        self._inner, self._budget, self._lane = inner, budget, lane

    @property
    def instruments(self):
        return _ArbitratedService(self._inner.instruments, self._budget, SERVICE_INSTRUMENTS, self._lane)

    @property
    def market_data(self):
        return _ArbitratedService(self._inner.market_data, self._budget, SERVICE_QUOTES, self._lane)

    def __getattr__(self, name):
        return getattr(self._inner, name)
