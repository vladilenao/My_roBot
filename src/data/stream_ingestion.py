"""Stream-полоса live-ingestion: подтверждённые подписки, здоровье, reconnect.

Отдельный модуль нужен из-за отсутствующего контракта жизненного цикла
bidi-подписки (design 2, 3, 10): у unary provider нет ACK, поколения
соединения, ограниченного буфера и fallback. Reader не вызывает торговый
контур: решённые события передаются единственному owner (кэшу).

Состояние соединения отделено от готовности данных: ACTIVE-соединение с ping
не означает READY-пару, а тихая сессия не означает обрыв.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from datetime import datetime, timezone

from src.api.retry import _is_rate_limited, rate_limit_reset_secs
from src.data.ingestion_settings import MarketDataSettings
from src.data.live_state import StreamClosureEvidence
from src.logging_setup import get_logger

log = get_logger(__name__)

SUBSCRIPTION_STATUS_SUCCESS = 1
SUBSCRIPTION_ACTION_SUBSCRIBE = 1
# Человекочитаемые причины отказа SubscriptionStatus (SDK enum значения).
SUBSCRIPTION_FAILURES = {
    2: "instrument_not_found", 3: "invalid_interval", 4: "limit_exceeded",
    5: "invalid_subscription", 6: "too_many_requests", 7: "invalid_source",
}
PERMANENT_CODES = {"UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_ARGUMENT", "NOT_FOUND"}
# Отказы, которые разумно повторить на новом поколении соединения; неверный
# UID/интервал/источник остаются отклонёнными с причиной.
TRANSIENT_FAILURES = {"ack_timeout", "too_many_requests", "limit_exceeded", "over_capacity", "subscription_missing"}
BATCH = 100


def _code(exc):
    code = getattr(exc, "code", None)
    try:
        value = code() if callable(code) else code
    except Exception:
        value = None
    return getattr(value, "name", str(value or ""))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StreamSupervisor:
    """Владелец stream-сессии одного соединения с поколениями (epoch)."""

    def __init__(self, cache, worker, session_factory, *, settings: MarketDataSettings | None = None,
                 clock=time.monotonic, wall=_now, sleep=None, jitter=random.random, publish=None):
        self._cache = cache
        self._worker = worker
        self._factory = session_factory
        self.settings = settings or MarketDataSettings()
        self._clock = clock
        self._wall = wall
        self._jitter = jitter
        self._publish = publish or (lambda kind, details: None)
        self._stopped = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stopped.wait(seconds))
        self._lock = threading.RLock()
        self._session = None
        self.epoch = 0
        self.connection = "DISCONNECTED"
        self._desired = set()
        self.pairs = {}
        self._buffer = deque()
        self._buffer_bytes = 0
        self.overflows = 0
        self._failures = 0
        self._control_sent = deque()
        self._last_response = None
        self._last_ping = None
        self._check_pending = None
        self._last_check = None
        self.fallback = False
        self.transitions = []
        self.last_error = ""
        self._last_open = {}

    # ── roster и состояние ──
    def set_roster(self, pairs):
        with self._lock:
            self._desired = set(pairs)
            for pair in self._desired:
                self.pairs.setdefault(pair, {"status": "desired", "epoch": None, "reason": "", "sent_at": None})
            self._publish_membership()

    def status(self):
        with self._lock:
            return {"connection": self.connection, "epoch": self.epoch, "fallback": self.fallback,
                    "buffer_events": len(self._buffer), "buffer_bytes": self._buffer_bytes,
                    "overflows": self.overflows, "last_error": self.last_error,
                    "last_ping": self._last_ping, "transitions": list(self.transitions[-20:]),
                    "pairs": {f"{uid}:{tf}": dict(info) for (uid, tf), info in sorted(self.pairs.items())}}

    def startup_complete(self):
        """Каждая пара подтверждена/handoff либо отклонена; без связи — fallback.

        Если соединение не установлено, а fallback разрешён, пары уже
        обслуживает polling того же owner: ожидание старта не бесконечно.
        """
        self.sync_roster()
        with self._lock:
            if self.settings.stream_fallback_polling and self.connection in {"BACKOFF", "FAILED"}:
                return True
            return all(info["status"] in {"ready", "failed"} for pair, info in self.pairs.items()
                       if pair in self._desired)

    def startup_pending(self):
        """Пары потока, которых ждёт ``startup_complete``: (UID, ТФ) → статус."""
        self.sync_roster()
        with self._lock:
            if self.settings.stream_fallback_polling and self.connection in {"BACKOFF", "FAILED"}:
                return {}
            return {pair: f"поток: {info['status']}" for pair, info in sorted(self.pairs.items())
                    if pair in self._desired and info["status"] not in {"ready", "failed"}}

    def sync_roster(self):
        """Желаемые подписки — зарегистрированные owner пары по UID."""
        pairs = {(pair["uid"], pair["timeframe"]) for pair in self._cache.live_pairs()}
        with self._lock:
            if pairs != self._desired:
                self.set_roster(pairs)

    def _transition(self, kind, **details):
        entry = {"at": self._wall().isoformat(), "kind": kind, **details}
        self.transitions.append(entry)
        del self.transitions[:-100]
        log.info("Live-stream: %s %s", kind, details)
        self._publish(kind, details)

    def _publish_membership(self):
        ready = {pair for pair, info in self.pairs.items() if info["status"] == "ready"}
        held = set()
        if not self.settings.stream_fallback_polling:
            # Без fallback неподтверждённая пара удерживается, а не polling.
            held = {pair for pair in self._desired if pair not in ready}
        self._worker.set_stream_pairs(ready, held=held)

    # ── жизненный цикл ──
    def run(self):
        """Цикл соединений до остановки; вызывается фоновой stream-полосой."""
        while not self._stopped.is_set():
            permanent = False
            try:
                self._connect_and_read()
                self._failures = 0 if self.connection == "ACTIVE" else self._failures + 1
            except Exception as exc:
                if self._stopped.is_set():
                    break
                code = _code(exc)
                permanent = code in PERMANENT_CODES
                self.last_error = f"{code}: {exc}" if code else str(exc)
                self._failures += 1
                log.warning("Live-stream ошибка (%s): %s", code or "transport", exc)
                delay = self._delay(exc, permanent)
            else:
                delay = self._delay(None, False)
            self._disconnected("permanent_error" if permanent else "disconnect")
            if self._stopped.is_set():
                break
            self.connection = "FAILED" if permanent else "BACKOFF"
            self._sleep(delay)
        self.connection = "STOPPING"

    def close(self):
        self._stopped.set()
        session = self._session
        if session is not None:
            # cancel прерывает блокирующее чтение; stop не ждёт будущей свечи.
            session.close()

    def _delay(self, exc, permanent):
        settings = self.settings
        if permanent:
            # Неизменный неверный запрос не повторяется быстро: cooldown/max.
            return max(settings.stream_control_cooldown_secs, settings.stream_reconnect_max_secs)
        if exc is not None and _is_rate_limited(exc):
            reset = rate_limit_reset_secs(exc) or settings.stream_reconnect_max_secs
            return max(reset, settings.stream_reconnect_initial_secs)
        base = min(settings.stream_reconnect_max_secs,
                   settings.stream_reconnect_initial_secs * (2 ** max(0, self._failures - 1)))
        spread = settings.stream_reconnect_jitter_pct / 100.0
        return max(0.0, base * (1 + spread * (2 * self._jitter() - 1)))

    def _connect_and_read(self):
        with self._lock:
            self.connection = "CONNECTING"
            session = self._factory()
            self._session = session
            self.epoch += 1
            epoch = self.epoch
        session.open()
        session.configure_ping(self.settings.stream_ping_interval_ms)
        self.sync_roster()
        with self._lock:
            self.connection = "SUBSCRIBING"
            self._last_response = self._clock()
            for info in self.pairs.values():
                if info["status"] == "failed" and info["reason"] in TRANSIENT_FAILURES:
                    info["status"] = "desired"
            self._transition("stream_epoch", epoch=epoch)
            self._subscribe(session, epoch)
        applier = threading.Thread(target=self._apply_loop, args=(session, epoch), name="live-stream-apply", daemon=True)
        applier.start()
        try:
            for response in session:
                if self._stopped.is_set() or self.epoch != epoch:
                    return
                self._receive(response, epoch)
        finally:
            with self._lock:
                self._session = None
            session.close()
            applier.join(timeout=5)

    # ── управление подписками ──
    def _control_allowed(self, count):
        now = self._clock()
        while self._control_sent and now - self._control_sent[0] >= 60:
            self._control_sent.popleft()
        return len(self._control_sent) + count <= self.settings.stream_control_requests_per_minute

    def _subscribe(self, session, epoch):
        now = self._clock()
        for info in self.pairs.values():
            # Отказ по частоте control-запросов: конечная пауза, затем повтор.
            if info["status"] == "failed" and info["reason"] == "too_many_requests" and now >= info.get("retry_at", 0):
                info["status"] = "desired"
        # При нехватке ёмкости первыми подписываются базовая 1m и рабочие ТФ;
        # оставшиеся пары объявленно обслуживает polling (не теряются).
        rank = {"1m": 0, "5m": 1, "15m": 1, "30m": 2, "1h": 1, "4h": 2, "1d": 2, "1w": 3, "1M": 3}
        pending = sorted((pair for pair in self._desired if self.pairs[pair]["status"] == "desired"),
                         key=lambda pair: (rank.get(pair[1], 4), pair))
        capacity = self.settings.stream_subscription_soft_limit
        active = sum(1 for info in self.pairs.values() if info["status"] in {"sent", "acknowledged", "ready"} and info["epoch"] == epoch)
        for pair in pending[max(0, capacity - active):]:
            self._fail(pair, "over_capacity")
        pending = pending[:max(0, capacity - active)]
        for start in range(0, len(pending), BATCH):
            batch = pending[start:start + BATCH]
            if not self._control_allowed(1):
                break
            session.subscribe(batch)
            self._control_sent.append(self._clock())
            for pair in batch:
                self.pairs[pair].update(status="sent", epoch=epoch, reason="", sent_at=self._clock())

    def _fail(self, pair, reason):
        info = self.pairs.setdefault(pair, {"status": "desired", "epoch": None, "reason": "", "sent_at": None})
        info.update(status="failed", reason=reason)
        self._cache.set_stream_evidence(*pair, None)
        self._cache.set_live_handoff(*pair, transport="polling" if self.settings.stream_fallback_polling else "stream",
                                     epoch=None, ready=self.settings.stream_fallback_polling)
        self._worker.cancel_handoff(*pair)
        if self.settings.stream_fallback_polling:
            self.fallback = True
            self._transition("stream_fallback", pair=f"{pair[0]}:{pair[1]}", reason=reason)
        else:
            self._transition("stream_hold", pair=f"{pair[0]}:{pair[1]}", reason=reason)
        self._publish_membership()

    # ── входящие ответы ──
    def _receive(self, response, epoch):
        with self._lock:
            self._last_response = self._clock()
            if response.kind == "subscribe_candles_response":
                for ack in response.acknowledgements:
                    self._acknowledge(ack, epoch)
                if self.connection == "SUBSCRIBING" and any(info["epoch"] == epoch and info["status"] in {"acknowledged", "ready"}
                                                            for info in self.pairs.values()):
                    self.connection = "ACTIVE"
                return
            if response.kind == "ping":
                self._last_ping = response.server_time
                return
            if response.kind != "candle" or response.candle is None:
                return
            candle = response.candle
            pair = (candle.uid, candle.timeframe)
            info = self.pairs.get(pair)
            if info is None or info["epoch"] != epoch or info["status"] not in {"acknowledged", "ready"}:
                # Событие до ACK этого поколения не открывает readiness; его
                # интервал согласует исторический overlap handoff.
                return
            size = 256 + len(candle.uid)
            if (len(self._buffer) >= self.settings.stream_buffer_max_events
                    or self._buffer_bytes + size > self.settings.stream_buffer_max_bytes):
                self.overflows += 1
                self._loss(pair, candle.open_time, "buffer_overflow")
                return
            self._buffer.append((epoch, candle, self._wall(), size))
            self._buffer_bytes += size

    def _acknowledge(self, ack, epoch):
        pair = (ack.uid, ack.timeframe)
        if ack.timeframe is None or pair not in self.pairs:
            return
        info = self.pairs[pair]
        if self._check_pending is not None:
            self._check_pending.discard(pair)
        if ack.action != SUBSCRIPTION_ACTION_SUBSCRIBE:
            return
        if ack.status != SUBSCRIPTION_STATUS_SUCCESS:
            reason = SUBSCRIPTION_FAILURES.get(ack.status, f"status_{ack.status}")
            if reason == "too_many_requests":
                info["retry_at"] = self._clock() + self.settings.stream_control_cooldown_secs
            return self._fail(pair, reason)
        if not ack.waiting_close or ack.source not in (None, 0):
            # Иной режим закрытия или source-profile не подтверждает закрытие.
            return self._fail(pair, "invalid_source" if ack.source not in (None, 0) else "waiting_close_not_confirmed")
        if info["status"] in {"acknowledged", "ready"} and info["epoch"] == epoch:
            return
        evidence = StreamClosureEvidence(ack.uid, ack.timeframe, epoch, ack.status, ack.action,
                                         ack.waiting_close, 0, ack.source)
        self._cache.set_stream_evidence(*pair, evidence)
        self._cache.set_live_handoff(*pair, transport="stream", epoch=epoch, ready=False)
        info.update(status="acknowledged", epoch=epoch, reason="")
        self._worker.request_handoff(*pair, epoch, acknowledged_at=self._wall(), on_ready=self._handoff_ready)

    def _handoff_ready(self, uid, timeframe, epoch):
        with self._lock:
            info = self.pairs.get((uid, timeframe))
            if info is None or info["epoch"] != epoch or epoch != self.epoch or info["status"] != "acknowledged":
                return  # устаревшее поколение не открывает готовность нового
            info["status"] = "ready"
            self._cache.set_live_handoff(uid, timeframe, transport="stream", epoch=epoch, ready=True)
            self._transition("stream_ready", pair=f"{uid}:{timeframe}", epoch=epoch)
            self._publish_membership()

    def _loss(self, pair, since, reason):
        """Известная потеря доставки: recovery сразу, readiness пары закрыт."""
        self._worker.note_loss(*pair, since, reason=reason)
        self._transition("stream_loss", pair=f"{pair[0]}:{pair[1]}", reason=reason)

    def _disconnected(self, reason):
        with self._lock:
            self.connection = "DISCONNECTED"
            for pair, info in self.pairs.items():
                if info["status"] in {"sent", "acknowledged", "ready"}:
                    was_ready = info["status"] == "ready"
                    info.update(status="desired", reason=reason)
                    self._cache.set_stream_evidence(*pair, None)
                    self._cache.set_live_handoff(*pair, transport="stream", epoch=None, ready=False)
                    self._worker.cancel_handoff(*pair)
                    if was_ready:
                        since = self._last_open.get(pair)
                        if since is not None:
                            self._loss(pair, since, reason)
            self._buffer.clear()
            self._buffer_bytes = 0
            if self.settings.stream_fallback_polling and self._desired:
                self.fallback = True
                self._transition("stream_fallback", reason=reason)
            self._publish_membership()

    # ── owner-применение, health и сверка членства ──
    def _apply_loop(self, session, epoch):
        while not self._stopped.is_set() and self.epoch == epoch and self._session is session:
            item = None
            with self._lock:
                if self._buffer:
                    item = self._buffer.popleft()
                    self._buffer_bytes -= item[3]
            if item is not None:
                item_epoch, candle, received_at, _ = item
                try:
                    self._cache.accept_stream_candle(candle, epoch=item_epoch, received_at=received_at)
                    self._last_open[candle.uid, candle.timeframe] = candle.open_time
                except Exception as exc:
                    log.warning("Live-stream: свеча %s %s не принята: %s", candle.uid, candle.timeframe, exc)
                continue
            if not self._watch(session, epoch):
                return
            self._stopped.wait(0.05)

    def _watch(self, session, epoch):
        now = self._clock()
        self.sync_roster()
        with self._lock:
            if self._last_response is not None and now - self._last_response > self.settings.stream_health_timeout_seconds:
                self.last_error = "health_timeout"
                self._transition("stream_health_timeout", epoch=epoch)
                session.close()
                return False
            for pair, info in self.pairs.items():
                if info["status"] == "sent" and info["sent_at"] is not None and now - info["sent_at"] > self.settings.stream_ack_timeout_seconds:
                    self._fail(pair, "ack_timeout")
            if self._check_pending is not None and now - self._last_check > self.settings.stream_ack_timeout_seconds:
                for pair in sorted(self._check_pending):
                    # Забытая подписка: повторная подписка и recovery пропуска.
                    info = self.pairs[pair]
                    info.update(status="desired", reason="subscription_missing")
                    self._cache.set_live_handoff(*pair, transport="stream", epoch=None, ready=False)
                    since = self._last_open.get(pair)
                    if since is not None:
                        self._loss(pair, since, "subscription_missing")
                self._check_pending = None
                self._publish_membership()
                self._subscribe(session, epoch)
            elif (self._check_pending is None and self.connection == "ACTIVE"
                  and (self._last_check is None or now - self._last_check >= self.settings.subscription_check_interval_secs)
                  and self._control_allowed(1)):
                self._check_pending = {pair for pair, info in self.pairs.items() if info["status"] in {"acknowledged", "ready"}}
                self._last_check = now
                session.subscriptions()
                self._control_sent.append(now)
            if any(info["status"] == "desired" for pair, info in self.pairs.items() if pair in self._desired):
                self._subscribe(session, epoch)
        return True
