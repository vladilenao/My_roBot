import json
import sqlite3
from dataclasses import replace
from io import StringIO

import pytest
import requests

from src.events.visual import serializable
from src.notifier.console import ConsoleChannel
from src.notifier.telegram import TelegramChannel
from src.notifier.telegram_chart import build_scene, render_png
from src.notifier.telegram_delivery import DeliveryRepository, namespace
from src.notifier.telegram_html import split_html, visible_text
from src.notifier.telegram_templates import render
from src.notifier.telegram_transport import PHOTO_REQUEST_MAX_BYTES
from tests.support.telegram import demo_event


class Server:
    def __init__(self):
        self.calls = []
        self.fail = {}
        self.chat = {"id": -10012345, "type": "channel", "username": "test_channel"}

    def post(self, url, data, timeout, files=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "data": data, "photo": files["photo"][1].read() if files else None})
        failure = self.fail.get(method)
        if isinstance(failure, list):
            # Последовательность исходов: первый вызов падает, дальше успех.
            failure = failure.pop(0) if failure else None
        if isinstance(failure, Exception):
            raise failure
        body = failure or {"ok": True, "result": self.chat if method == "getChat" else {"message_id": len(self.calls)}}
        class Response:
            status_code = 429 if body.get("parameters") else 200 if body.get("ok") else 400
            def json(self):
                return body
        return Response()


@pytest.fixture
def server(monkeypatch):
    server = Server()
    monkeypatch.setattr(requests, "post", server.post)
    return server


def channel(path, **kwargs):
    return TelegramChannel(bot_token="123:secret", channel_id="-10012345", cloudflare_url="https://proxy.test",
                           delivery_path=path, tz_offset_hours=3, **kwargs)


def test_lifecycle_posts_preserve_plan_and_final_finances(server, tmp_path):
    instance = channel(tmp_path / "delivery.db")
    for stage in ("plan", "entry", "tp1", "stop", "final"):
        instance.handle(demo_event(stage))
    instance.close(10)
    photos = [c for c in server.calls if c["method"] == "sendPhoto"]
    assert len(photos) == 3
    assert all(c["photo"].startswith(b"\x89PNG") for c in photos)
    with sqlite3.connect(tmp_path / "delivery.db") as connection:
        saved = [row[0] for row in connection.execute("SELECT content FROM telegram_files ORDER BY file_id")]
        navigation = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM operation_navigation ORDER BY operation_id")]
    assert saved == [c["photo"] for c in photos]
    assert navigation[0] == {"root": True, "roles": []}
    assert navigation[-1] == {"root": False, "roles": ["Итог", "ЦЕЛЬ2"]}
    for call in photos:
        prepared = requests.Request("POST", "https://proxy.test/sendPhoto", data=call["data"],
                                    files={"photo": ("trade.png", call["photo"], "image/png")}).prepare()
        assert len(prepared.body) <= PHOTO_REQUEST_MAX_BYTES
    final = visible_text(photos[-1]["data"]["caption"])
    assert "+98 ₽" in final and "−6 ₽" in final.replace("-", "−") and "+92 ₽" in final
    edits = [c for c in server.calls if c["method"] == "editMessageCaption"]
    assert len(edits) == 4
    assert "Закрыта" in edits[-1]["data"]["caption"]
    assert "297.00" in edits[-1]["data"]["caption"]
    buttons = json.loads(edits[-1]["data"]["reply_markup"])["inline_keyboard"]
    assert {b["text"] for row in buttons for b in row} >= {"ЦЕЛЬ1", "ЦЕЛЬ2", "СТОП", "Итог"}
    assert not any(c["method"] == "editMessageMedia" for c in server.calls)
    for c in photos[1:]:
        button = json.loads(c["data"]["reply_markup"])["inline_keyboard"][0][0]
        assert button["url"] == "https://t.me/test_channel/1"


def test_compression_failure_preserves_text_and_sends_no_photo(server, tmp_path, monkeypatch):
    def too_large(*args):
        raise ValueError("Не помещается")
    monkeypatch.setattr("src.notifier.telegram.compact_png", too_large)
    instance = channel(tmp_path / "delivery.db")
    instance.handle(demo_event())
    instance.close(10)
    assert not any(c["method"] == "sendPhoto" for c in server.calls)
    texts = [c["data"]["text"] for c in server.calls if c["method"] == "sendMessage"]
    assert "297.00" in "\n".join(texts)
    assert "ЦЕЛЬ1" in "\n".join(texts)


def test_restart_duplicate_stale_and_two_trades(server, tmp_path):
    path = tmp_path / "delivery.db"
    instance = channel(path)
    for stage in ("plan", "tp1"):
        instance.handle(demo_event(stage, candles=False))
    instance.close(5)
    count = len(server.calls)
    instance = channel(path)
    instance.handle(demo_event("tp1", candles=False))
    instance.handle(demo_event("entry", candles=False))
    instance.handle(demo_event("stop", candles=False))
    instance.handle(demo_event("plan", candles=False, trade_id="second"))
    instance.close(5)
    posts = [c for c in server.calls[count:] if c["method"] == "sendMessage"]
    assert len(posts) == 2
    assert "СТОП перенесён" in posts[0]["data"]["text"]
    assert "https://t.me/test_channel/1" in posts[0]["data"]["reply_markup"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades WHERE root_id IS NOT NULL").fetchone()[0] == 2


@pytest.mark.parametrize("failure", [requests.Timeout("https://proxy.test/bot123:secret/sendPhoto"),
                                  {"ok": False, "description": "bad photo"},
                                  {"ok": False, "parameters": {"retry_after": 12}}])
def test_uncertain_failed_and_rate_limited_photo_never_republished(server, tmp_path, failure, caplog):
    server.fail["sendPhoto"] = failure
    path = tmp_path / "delivery.db"
    for _ in range(2):
        instance = channel(path)
        instance.handle(demo_event())
        instance.close(5)
    assert [c["method"] for c in server.calls] == ["sendPhoto"]
    assert "secret" not in caplog.text
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT root_id FROM trades").fetchone()[0] is None


def test_uncertain_timeout_is_retried_once_and_confirmed(server, tmp_path, monkeypatch):
    monkeypatch.setattr("src.notifier.telegram.RETRY_DELAY", 0.0)
    server.fail["sendPhoto"] = [requests.Timeout("https://proxy.test/bot123:secret/sendPhoto"), None]
    path = tmp_path / "delivery.db"
    instance = channel(path, max_transport_attempts=2)
    instance.handle(demo_event())
    instance.close(5)
    assert [c["method"] for c in server.calls if c["method"] == "sendPhoto"] == ["sendPhoto", "sendPhoto"]
    with sqlite3.connect(path) as conn:
        status, message_id = conn.execute(
            "SELECT status,message_id FROM attempts WHERE operation_key LIKE '%:post'"
        ).fetchone()
        assert status == "confirmed" and isinstance(message_id, int)
        assert conn.execute("SELECT root_id FROM trades").fetchone()[0] is not None


def test_failed_response_is_never_retried_even_when_allowed(server, tmp_path, monkeypatch):
    monkeypatch.setattr("src.notifier.telegram.RETRY_DELAY", 0.0)
    server.fail["sendPhoto"] = {"ok": False, "description": "bad photo"}
    instance = channel(tmp_path / "delivery.db", max_transport_attempts=2)
    instance.handle(demo_event())
    instance.close(5)
    assert [c["method"] for c in server.calls] == ["sendPhoto"]


def test_second_uncertainty_stops_retrying(server, tmp_path, monkeypatch):
    monkeypatch.setattr("src.notifier.telegram.RETRY_DELAY", 0.0)
    timeout = requests.Timeout("https://proxy.test/bot123:secret/sendPhoto")
    server.fail["sendPhoto"] = [timeout, timeout]
    path = tmp_path / "delivery.db"
    instance = channel(path, max_transport_attempts=2)
    instance.handle(demo_event())
    instance.close(5)
    assert [c["method"] for c in server.calls] == ["sendPhoto", "sendPhoto"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status,message_id FROM attempts").fetchone() == ("uncertain", None)
        assert conn.execute("SELECT root_id FROM trades").fetchone()[0] is None


def test_missing_root_metadata_failure_and_root_edit_failure(server, tmp_path):
    instance = channel(tmp_path / "delivery.db")
    instance.handle(demo_event("tp1", candles=False))
    instance.close(5)
    assert [c["method"] for c in server.calls] == ["sendMessage"]
    assert json.loads(server.calls[0]["data"]["reply_markup"])["inline_keyboard"] == []
    server.fail["getChat"] = {"ok": False, "description": "not found"}
    server.fail["editMessageText"] = {"ok": False, "description": "deleted"}
    instance = channel(tmp_path / "other.db")
    for stage in ("plan", "tp1", "stop"):
        instance.handle(demo_event(stage, candles=False))
    instance.close(5)
    assert len([c for c in server.calls if c["method"] == "sendMessage"]) == 4
    assert len([c for c in server.calls if c["method"] == "editMessageText"]) == 2


def test_private_channel_links_and_benign_edit(server, tmp_path):
    server.chat.pop("username")
    server.fail["editMessageText"] = {"ok": False, "description": "Bad Request: message is not modified"}
    instance = channel(tmp_path / "delivery.db")
    for stage in ("plan", "tp1"):
        instance.handle(demo_event(stage, candles=False))
    instance.close(5)
    post = next(c for c in server.calls if "ЦЕЛЬ1" in c["data"].get("text", "") and c["method"] == "sendMessage" and "Получено" in c["data"]["text"])
    assert "https://t.me/c/12345/1" in post["data"]["reply_markup"]


def test_local_renderer_failure_sends_one_text_and_console_unchanged(server, tmp_path, monkeypatch):
    def failure(scene):
        raise RuntimeError("font")
    monkeypatch.setattr("src.notifier.telegram.render_png", failure)
    instance = channel(tmp_path / "delivery.db")
    event = demo_event("final")
    stream = StringIO()
    ConsoleChannel(stream=stream).handle(event)
    console_before = stream.getvalue()
    instance.handle(event)
    instance.close(5)
    assert [c["method"] for c in server.calls] == ["sendMessage"]
    assert "+92 ₽" in server.calls[0]["data"]["text"]
    assert stream.getvalue() == console_before and "<b>" not in console_before


def test_html_entities_long_lines_and_unicode_preserve_content():
    text = '<b>Заголовок\n<i>' + '😀&amp;&lt;&gt;' * 1500 + '</i></b>'
    chunks = split_html(text, 1024)
    assert ''.join(visible_text(c) for c in chunks) == visible_text(text)
    assert all(len(visible_text(c).encode('utf-16-le')) // 2 <= 1024 for c in chunks)
    assert all(c.startswith('<b>') and c.endswith('</b>') for c in chunks)


def test_scene_short_mirrors_levels_and_markers_and_has_true_effective_step():
    scene = build_scene(demo_event("final", side="SELL"), 3)
    assert scene["levels"][0]["price"] == "300.00"
    assert scene["original_stop"] == "303.00"
    assert scene["markers"][0]["side"] == "SELL"
    assert scene["stop_steps"][-1] == {"time": "2026-10-06T08:15:00", "price": "302.75"}
    assert "+92 ₽" in scene["result"]
    assert render_png(scene).startswith(b"\x89PNG")


def test_same_price_new_fill_is_not_duplicate_and_sender_namespace_isolated(server, tmp_path):
    path = tmp_path / "delivery.db"
    event = demo_event("tp1", candles=False)
    visual = serializable(event.get("visual"))
    visual.update(event_key="second-fill", sequence=3, revision=3)
    instance = channel(path)
    instance.handle(event)
    instance.handle(replace(event, payload={**event.payload, "visual": visual}))
    instance.handle(event)
    instance.close(5)
    assert len(server.calls) == 2
    assert namespace("123:rotated", "-10012345") == namespace("123:secret", "-10012345")
    assert namespace("124:secret", "-10012345") != namespace("123:secret", "-10012345")


def test_crash_attempt_is_uncertain_not_confirmation(tmp_path):
    path = tmp_path / "delivery.db"
    repo = DeliveryRepository(path, "namespace")
    assert repo.begin_attempt("post")
    repo.close()
    repo = DeliveryRepository(path, "namespace")
    assert not repo.begin_attempt("post")
    assert repo.connection.execute("SELECT status,message_id FROM attempts").fetchone() == ("uncertain", None)
    repo.close()


def test_financial_unknown_raw_and_configured_are_not_broker_facts():
    event = demo_event("final")
    visual = serializable(event.get("visual"))
    visual["financial"].update(fees_known=False, fees_source="unknown")
    assert "+92 ₽" not in render(replace(event, payload={**event.payload, "visual": visual}))
    visual["financial"].update(fees_known=True, fees_source="configured")
    assert "Комиссии (оценка)" in render(replace(event, payload={**event.payload, "visual": visual}))
    visual["financial"].update(units="RAW")
    assert "Рублёвый итог недоступен" in render(replace(event, payload={**event.payload, "visual": visual}))
