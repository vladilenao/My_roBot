"""Интеграция outbox и Redis Streams на настоящем Redis в Docker."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from datetime import timedelta

import pytest
import redis

from src.data.market_store import MarketDataStore
from src.market_structure.worker import PatternWorker


@pytest.fixture
def redis_stream():
    if shutil.which("docker") is None:
        pytest.skip("для интеграционного теста Redis нужен Docker")
    name = f"myrobot-redis-{uuid.uuid4().hex}"
    started = subprocess.run(
        ["docker", "run", "--rm", "-d", "--name", name, "-p", "127.0.0.1::6379", "redis:7",
         "redis-server", "--appendonly", "yes", "--appendfsync", "always", "--save", ""],
        text=True, capture_output=True,
    )
    if started.returncode:
        pytest.skip(f"не удалось запустить Redis в Docker: {started.stderr.strip()}")
    try:
        published = subprocess.check_output(["docker", "port", name, "6379/tcp"], text=True).strip()
        port = int(published.rsplit(":", 1)[1])
        client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                client.ping()
                break
            except redis.RedisError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield client
    finally:
        subprocess.run(["docker", "rm", "-f", name], text=True, capture_output=True, check=False)


def _event(event_id: str) -> str:
    return json.dumps({
        "schema_version": 1,
        "event_id": event_id,
        "ticker": "SBER",
        "timeframe": "1m",
        "timestamp": "2026-10-14T10:00:00Z",
        "source": "candle_patterns",
        "event_type": "bullish_engulfing",
        "direction": "long",
        "payload": {"body_overlap_ratio": 1.0,
                    "previous_candle": {"open": 10, "high": 11, "low": 8, "close": 9},
                    "current_candle": {"open": 8, "high": 12, "low": 7, "close": 11}},
    }, ensure_ascii=False)


def test_redis_stream_outbox_consumer_pending_ack_and_retention(redis_stream, tmp_path):
    stream = "market.events"
    seconds, microseconds = redis_stream.time()
    expired_ms = int((seconds + microseconds / 1_000_000 - timedelta(days=91).total_seconds()) * 1_000)
    redis_stream.xadd(stream, {"event_id": "expired"}, id=f"{expired_ms}-0")

    path = tmp_path / "market-data.sqlite3"
    store = MarketDataStore(path)
    store.enqueue_pattern_event(event_id="event-1", payload_json=_event("event-1"))
    store.enqueue_pattern_event(event_id="event-1", payload_json=_event("event-1"))
    worker = PatternWorker(store, redis_stream, retention_days=90)
    worker._publish_outbox()

    entries = redis_stream.xrange(stream)
    assert len(entries) == 1
    event_entry_id, fields = entries[0]
    assert fields["event_id"] == "event-1"
    assert "published_at" in fields

    redis_stream.xgroup_create(stream, "cluster-source-v1", id="0-0", mkstream=True)
    first = redis_stream.xreadgroup("cluster-source-v1", "cluster-source-main", {stream: ">"})
    assert first[0][1][0][0] == event_entry_id
    pending = redis_stream.xreadgroup("cluster-source-v1", "cluster-source-main", {stream: "0"})
    assert pending[0][1][0][0] == event_entry_id
    assert redis_stream.xack(stream, "cluster-source-v1", event_entry_id) == 1

    restarted = MarketDataStore(path)
    restarted.enqueue_pattern_event(event_id="event-2", payload_json=_event("event-2"))
    PatternWorker(restarted, redis_stream, retention_days=90)._publish_outbox()
    new = redis_stream.xreadgroup("cluster-source-v1", "cluster-source-main", {stream: ">"})
    assert new[0][1][0][1]["event_id"] == "event-2"
