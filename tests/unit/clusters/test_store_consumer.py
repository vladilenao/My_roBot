"""Хранилище и чтение cluster.events (change add-cluster-filter, design D1–D2)."""

from datetime import datetime, timezone

import pytest

from src.clusters.consumer import ClusterEventsConsumer, catch_up
from src.clusters.store import ClusterEventStore, copy_store
from tests.unit.clusters.helpers import stream_fields


class FakeRedis:
    """Поток в памяти: группа, pending потребителя, XACK, XRANGE."""

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.groups = {}
        self.pending = []
        self.acked = []
        self.delivered = 0
        self.fail = None

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if group in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups[group] = 0

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        if self.fail:
            raise self.fail
        if group not in self.groups:
            raise RuntimeError(f"NOGROUP No such key 'cluster.events' or consumer group '{group}'")
        (stream, start), = streams.items()
        if start == "0":
            batch = [entry for entry in self.entries if entry[0] in self.pending][:count]
        else:
            batch = self.entries[self.delivered:self.delivered + count]
            self.delivered += len(batch)
            self.pending.extend(entry[0] for entry in batch)
        return [[stream, batch]] if batch else []

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)
        self.pending = [item for item in self.pending if item not in ids]
        return len(ids)

    def xrange(self, stream, min="-", max="+", count=None):
        if self.fail:
            raise self.fail
        after = min[1:] if min.startswith("(") else None
        items = [e for e in self.entries if after is None or _order(e[0]) > _order(after)]
        return items[:count]


def _order(stream_id):
    ms, seq = stream_id.split("-")
    return int(ms), int(seq)


def _stream(n=3):
    return [(f"1792071600{i:03d}-0", stream_fields(f"{i:064x}")) for i in range(n)]


NOW = datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)


def _consumer(store, redis):
    return ClusterEventsConsumer(store, redis, stream="cluster.events", group="robot-v1", clock=lambda: NOW)


class TestStore:
    def test_row_keeps_fields_and_payload(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        store.write(_stream(1))

        stored = store.signals("BRX6", after=datetime(2026, 10, 15, tzinfo=timezone.utc), until=NOW)

        assert len(stored) == 1
        assert stored[0].secid == "BRX6"
        assert stored[0].direction == "short"
        assert stored[0].payload == {"ratio": 9.606}
        assert stored[0].fields["source"] == "cluster_source"

    def test_repeated_event_id_is_one_row(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")

        assert store.write(_stream(1)) == 1
        assert store.write(_stream(1)) == 0

    def test_confirmations_are_not_stored(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        confirmed = stream_fields("a" * 64, category="signal_result", event_type="signal_confirmed")
        revoked = stream_fields("b" * 64, category="signal_result", event_type="signal_revoked")

        assert store.write([("1-0", confirmed), ("2-0", revoked)]) == 1
        assert store.revoked(["b" * 64], until=NOW) == {"b" * 64}

    def test_last_quality(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        degraded = stream_fields("c" * 64, category="data_quality", event_type="data_degraded",
                                 published="2026-10-15T13:00:00Z")
        restored = stream_fields("d" * 64, category="data_quality", event_type="data_restored",
                                 published="2026-10-15T13:10:00Z")
        store.write([("1-0", degraded), ("2-0", restored)])

        assert store.last_quality("BRX6", until=datetime(2026, 10, 15, 13, 5, tzinfo=timezone.utc)) == "data_degraded"
        assert store.last_quality("BRX6", until=NOW) == "data_restored"

    def test_copy_for_history_reads_source_only(self, tmp_path):
        live = ClusterEventStore(tmp_path / "live.sqlite3")
        live.write(_stream(2))

        assert copy_store(live.path, tmp_path / "run.sqlite3")
        assert ClusterEventStore(tmp_path / "run.sqlite3").state("last_stream_id") == "1792071600001-0"
        assert not copy_store(tmp_path / "missing.sqlite3", tmp_path / "other.sqlite3")


class TestConsumer:
    def test_reads_pending_then_new_and_acks_after_write(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis(_stream(3))
        consumer = _consumer(store, redis)

        consumer.step()  # pending пуст
        consumer.step()  # новые

        assert redis.acked == [entry[0] for entry in _stream(3)]
        assert len(store.signals("BRX6", after=datetime(2026, 10, 15, tzinfo=timezone.utc), until=NOW)) == 3
        assert consumer.last_read_at == NOW

    def test_failed_write_is_not_acked(self, tmp_path, monkeypatch):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis(_stream(2))
        consumer = _consumer(store, redis)
        consumer.step()
        monkeypatch.setattr(store, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))

        with pytest.raises(OSError):
            consumer.step()

        assert redis.acked == []
        assert len(redis.pending) == 2

    def test_pending_is_redelivered_after_restart(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis(_stream(2))
        redis.groups["robot-v1"] = 0
        redis.pending = [entry[0] for entry in _stream(2)]
        redis.delivered = 2

        _consumer(store, redis).step()

        assert redis.acked == [entry[0] for entry in _stream(2)]

    def test_existing_group_is_reused(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis()
        redis.groups["robot-v1"] = 0

        assert _consumer(store, redis).step() == 0

    def test_redis_error_surfaces_for_retry(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis()
        redis.fail = ConnectionError("down")

        with pytest.raises(ConnectionError):
            _consumer(store, redis).step()

    def test_lost_group_is_recreated(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis(_stream(2))
        consumer = _consumer(store, redis)
        consumer.step()  # pending пуст
        consumer.step()  # новые
        # Redis пересоздан пустым: группы нет, поток наполняется заново.
        redis.__init__(_stream(3))

        assert consumer.step() == 0
        assert "robot-v1" in redis.groups
        consumer.step()  # pending пуст
        assert consumer.step() == 1  # повторы отсекает event_id

        assert redis.acked == [entry[0] for entry in _stream(3)]
        assert len(store.signals("BRX6", after=datetime(2026, 10, 15, tzinfo=timezone.utc), until=NOW)) == 3


class TestCatchUp:
    def test_incremental_range(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        redis = FakeRedis(_stream(2))

        assert catch_up(store, redis, stream="cluster.events") == 2
        redis.entries.extend(_stream(4)[2:])

        assert catch_up(store, redis, stream="cluster.events") == 2
        assert store.state("last_stream_id") == "1792071600003-0"

    def test_offline_redis_keeps_stored_events(self, tmp_path):
        store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
        store.write(_stream(1))
        redis = FakeRedis()
        redis.fail = ConnectionError("down")

        assert catch_up(store, redis, stream="cluster.events") == 0
        assert store.has_events("BRX6", until=NOW)
