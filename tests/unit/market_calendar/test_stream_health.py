from datetime import datetime
from types import SimpleNamespace

from src.data.ingestion_settings import MarketDataSettings
from src.data.stream_ingestion import StreamSupervisor


def test_quiet_break_keeps_stream_healthy_when_responses_arrive():
    clock = [0.0]

    class Cache:
        def live_pairs(self):
            return []

        def set_live_handoff(self, *args, **kwargs):
            raise AssertionError("Тихая сессия не должна менять handoff")

    class Worker:
        def set_stream_pairs(self, ready, held=()):
            pass

    session = SimpleNamespace(close=lambda: (_ for _ in ()).throw(AssertionError("stream не должен закрываться")), subscriptions=lambda: None)
    supervisor = StreamSupervisor(
        Cache(), Worker(), lambda: session,
        settings=MarketDataSettings(stream_health_timeout_seconds=45, subscription_check_interval_secs=300),
        clock=lambda: clock[0], wall=lambda: datetime(2026, 10, 13),
    )
    supervisor.connection = "ACTIVE"
    supervisor.epoch = 1
    supervisor._last_response = 0.0  # ping/ACK есть, свечей нет.
    supervisor.pairs["uid", "1m"] = {"status": "ready", "epoch": 1, "reason": "", "sent_at": None}
    supervisor._desired = {("uid", "1m")}

    assert supervisor._watch(session, 1) is True
    assert supervisor.pairs["uid", "1m"]["status"] == "ready"
