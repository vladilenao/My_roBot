"""Неблокирующий издатель свечных паттернов.

Торговый цикл только помещает закрытый бар в очередь. SQLite, recovery и Redis
используются только этим потоком.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from src.logging_setup import get_logger
from src.market_structure.patterns import PATTERNS

log = get_logger(__name__)
SOURCE = "candle_patterns"
SCHEMA_VERSION = 1


def _utc_iso(value: object) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PatternTask:
    ticker: str
    instrument_uid: str
    timeframe: str
    close_time: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.instrument_uid, self.timeframe, self.close_time


class PatternWorker:
    """Один daemon-поток с at-least-once публикацией через SQLite outbox."""

    def __init__(self, store, redis_client, *, recovery_requester: Callable[[str, str, object], None] | None = None,
                 source: str = "tbank_exchange", queue_maxsize: int = 1024,
                 retention_days: int = 90) -> None:
        self._store = store
        self._redis = redis_client
        self._recovery_requester = recovery_requester
        self._source = source
        self._queue: queue.Queue[PatternTask] = queue.Queue(maxsize=queue_maxsize)
        self._queue_maxsize = queue_maxsize
        self._pending: set[tuple[str, str, str]] = set()
        self._deferred: dict[tuple[str, str, str], tuple[PatternTask, float]] = {}
        self._watermarks: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="market-structure", daemon=True)
        self._retention_days = retention_days
        self._last_trimmed: datetime | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def submit(self, *, ticker: str, instrument_uid: str, timeframe: str, close_time: object) -> bool:
        task = PatternTask(ticker, instrument_uid, timeframe, _utc_iso(close_time))
        with self._lock:
            pair = task.instrument_uid, task.timeframe
            if task.key in self._pending or task.close_time <= self._watermarks.get(pair, ""):
                return False
            if len(self._pending) >= self._queue_maxsize:
                log.warning("Очередь market_structure заполнена: %s %s %s", ticker, timeframe, task.close_time)
                return False
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                log.warning("Очередь market_structure заполнена: %s %s %s", ticker, timeframe, task.close_time)
                return False
            self._pending.add(task.key)
        return True

    def prime(self, *, instrument_uid: str, timeframe: str, close_time: object) -> None:
        """Отмечает стартовый бар, чтобы запуск не превращался в replay истории."""
        value = _utc_iso(close_time)
        with self._lock:
            key = instrument_uid, timeframe
            self._watermarks[key] = max(value, self._watermarks.get(key, ""))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._enqueue_due_deferred()
            self._publish_outbox()
            try:
                task = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                completed = False
                completed = self._analyze(task)
            except Exception:
                log.exception("Ошибка анализа market_structure: %s %s %s", task.ticker, task.timeframe, task.close_time)
            finally:
                with self._lock:
                    pair = task.instrument_uid, task.timeframe
                    if completed:
                        self._pending.discard(task.key)
                        self._watermarks[pair] = max(task.close_time, self._watermarks.get(pair, ""))
                    else:
                        self._deferred[task.key] = (task, time.monotonic() + 1.0)
                self._queue.task_done()

    def _enqueue_due_deferred(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = [key for key, (_, at) in self._deferred.items() if at <= now]
            for key in due:
                task, _ = self._deferred.pop(key)
                self._queue.put_nowait(task)

    def _analyze(self, task: PatternTask) -> bool:
        context = self._store.pattern_context(
            source=self._source, instrument_uid=task.instrument_uid,
            interval=task.timeframe, close_time=task.close_time,
        )
        if context["has_gap"]:
            if self._recovery_requester is not None:
                self._recovery_requester(task.instrument_uid, task.timeframe, context["missing_open_time"])
            return False
        candles = context["candles"]
        if len(candles) != 2:
            return True
        results = [result for pattern in PATTERNS if (result := pattern.analyze(candles)) is not None]
        if not results:
            log.debug("Свеча [%s] по [%s] проанализирована модулем market_structure, паттернов не обнаружено",
                      task.close_time, task.ticker)
            return True
        for result in results:
            event = self._event(task, result)
            self._store.enqueue_pattern_event(event_id=event["event_id"], payload_json=json.dumps(event, ensure_ascii=False))
        self._publish_outbox()
        return True

    def _event(self, task: PatternTask, result) -> dict[str, object]:
        identity = f"{SOURCE}/{SCHEMA_VERSION}/{task.ticker}/{task.timeframe}/{task.close_time}/{result.event_type}"
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "ticker": task.ticker,
            "timeframe": task.timeframe,
            "timestamp": task.close_time,
            "source": SOURCE,
            "event_type": result.event_type,
            "direction": result.direction,
            "payload": {
                "body_overlap_ratio": result.body_overlap_ratio,
                "previous_candle": self._ohlc(result.previous_candle),
                "current_candle": self._ohlc(result.current_candle),
            },
        }

    @staticmethod
    def _ohlc(candle) -> dict[str, float]:
        return {name: float(candle[name]) for name in ("open", "high", "low", "close")}

    def _publish_outbox(self) -> None:
        if self._redis is None:
            return
        for row in self._store.pending_pattern_events(limit=100):
            try:
                event = json.loads(row["payload_json"])
                event["published_at"] = self._redis_time()
                fields = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                          for key, value in event.items()}
                self._redis.xadd("market.events", fields)
            except Exception as exc:
                self._store.defer_pattern_event(row["event_id"], str(exc))
                return
            self._store.mark_pattern_event_delivered(row["event_id"])
        try:
            self._trim()
        except Exception:
            log.warning("Не удалось выполнить retention Redis market.events", exc_info=True)

    def _redis_time(self) -> str:
        seconds, microseconds = self._redis.time()
        return datetime.fromtimestamp(int(seconds) + int(microseconds) / 1_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _trim(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_trimmed is not None and now - self._last_trimmed < timedelta(days=1):
            return
        seconds, microseconds = self._redis.time()
        current = datetime.fromtimestamp(int(seconds) + int(microseconds) / 1_000_000, tz=timezone.utc)
        cutoff = int((current - timedelta(days=self._retention_days)).timestamp() * 1000)
        self._redis.xtrim("market.events", minid=f"{cutoff}-0", approximate=False)
        self._last_trimmed = now
