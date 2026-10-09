import json
import threading

import requests

from src.events.visual import bounded_history, market_snapshot
from src.notifier.telegram import TelegramChannel
from tests.support.telegram import demo_event
from tests.unit.notifier.test_visual_delivery import Server


def test_overflow_keeps_fresh_state_and_shutdown_discards_pending(tmp_path, monkeypatch, caplog):
    started, release = threading.Event(), threading.Event()
    server = Server()
    def post(*args, **kwargs):
        started.set()
        release.wait(2)
        return server.post(*args, **kwargs)
    monkeypatch.setattr(requests, "post", post)
    channel = TelegramChannel(bot_token="123:secret", channel_id="-10012345", cloudflare_url="https://proxy.test",
                              queue_size=1, delivery_path=tmp_path / "delivery.db")
    channel.handle(demo_event("plan", candles=False))
    assert started.wait(2)
    channel.handle(demo_event("tp1", candles=False))
    channel.handle(demo_event("final", candles=False))
    assert channel._dropped == 1
    channel.close(.01)
    release.set()
    channel._worker.join(2)
    assert not channel._worker.is_alive()
    assert len(server.calls) == 2  # initial send and metadata; queued final discarded
    assert "отброшено" in caplog.text


def test_delivery_initialization_error_isolates_channel(tmp_path, monkeypatch, caplog):
    server = Server()
    monkeypatch.setattr(requests, "post", server.post)
    channel = TelegramChannel(bot_token="123:secret", channel_id="chat", cloudflare_url="https://proxy.test",
                              delivery_path=tmp_path)  # directory cannot be opened as SQLite file
    channel._worker.join(2)
    assert not channel.enabled and not server.calls
    assert "delivery-состояние недоступно" in caplog.text


def test_long_caption_continuations_keep_root_identity_and_html(tmp_path, monkeypatch):
    server = Server()
    monkeypatch.setattr(requests, "post", server.post)
    event = demo_event("plan")
    # Проверяем разбивку подписи независимо от размера графика со 100 уровнями.
    from src.notifier.telegram_chart import build_scene, render_png
    photo = render_png(build_scene(event))
    monkeypatch.setattr("src.notifier.telegram.render_png", lambda scene: photo)
    from dataclasses import replace
    from src.events.visual import serializable
    visual = serializable(event.get("visual"))
    visual["plan"]["targets"] = [{"id": str(i), "number": i, "quantity": 1, "price": str(301+i)} for i in range(1, 101)]
    channel = TelegramChannel(bot_token="123:secret", channel_id="-10012345", cloudflare_url="https://proxy.test",
                              delivery_path=tmp_path / "delivery.db")
    channel.handle(replace(event, payload={**event.payload, "visual": visual}))
    channel.close(10)
    photos = [c for c in server.calls if c["method"] == "sendPhoto"]
    details = [c for c in server.calls if c["method"] == "sendMessage"]
    assert len(photos) == 1 and details
    assert all(json.loads(c["data"]["reply_markup"])["inline_keyboard"][0][0]["url"] == "https://t.me/test_channel/1" for c in details)
    assert "ЦЕЛЬ100" in "".join(c["data"]["text"] for c in details)


def test_stop_history_boundary_and_truncation_do_not_invent_early_steps():
    market = {"candles": [{"time": "2026-01-01T01:10:00"}], "limited": True, "gaps": False}
    stops = [{"key": str(i), "time": f"2026-01-01T01:{i:02}:00", "new": str(i), "reason": "trailing"} for i in range(59)]
    _, _, retained, result = bounded_history([], stops, market)
    assert result["boundary_stop"] == "10" and len(retained) == 59


def test_monthly_context_uses_calendar_boundary():
    from datetime import datetime
    import pandas as pd
    frame = pd.DataFrame({"datetime": [datetime(2026, 2, 1)], "open": [1], "low": [.9], "high": [1.2], "close": [1.1]})
    assert market_snapshot(frame, "1M", datetime(2026, 3, 1))["candles"]
    assert not market_snapshot(frame, "1M", datetime(2026, 2, 28))["candles"]
