"""Фоновая досылка Telegram-запросов, которые гарантированно не ушли."""

import json
import logging
import sqlite3
import time
from datetime import timedelta

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from src.notifier.telegram import TelegramChannel
from src.notifier.telegram_delivery import AUTO_RETRY_EXPIRED, DeliveryRepository, namespace
from src.notifier.telegram_transport import ApiResult
from tests.support.telegram import demo_event


def unsent():
    """Отказ в соединении: так requests сообщает, что запрос не ушёл."""
    return requests.ConnectionError(MaxRetryError(
        None, "/bot123:secret/sendPhoto",
        NewConnectionError(None, "Failed to establish a new connection: https://proxy.test")))


def read_timeout():
    return requests.ReadTimeout("HTTPSConnectionPool(host='proxy.test'): /bot123:secret/sendPhoto")


class Server:
    """Bot API: ``outcomes`` — исходы следующих вызовов по порядку, дальше успех."""

    def __init__(self):
        self.calls = []
        self.outcomes = {}
        self.down = False
        self.chat = {"id": -10012345, "type": "channel", "username": "test_channel"}

    def post(self, url, data, timeout, files=None):
        method = url.rsplit("/", 1)[-1]
        if self.down:
            raise unsent()
        pending = self.outcomes.get(method)
        failure = pending.pop(0) if pending else None
        if isinstance(failure, Exception):
            raise failure
        self.calls.append({"method": method, "data": data, "photo": files["photo"][1].read() if files else None})
        body = {"ok": True, "result": self.chat if method == "getChat" else {"message_id": len(self.calls)}}

        class Response:
            status_code = 200

            def json(self):
                return body
        return Response()

    def sent(self, method):
        return [call for call in self.calls if call["method"] == method]


@pytest.fixture
def server(monkeypatch):
    server = Server()
    monkeypatch.setattr(requests, "post", server.post)
    monkeypatch.setattr("src.notifier.telegram.RETRY_DELAY", 0.0)
    monkeypatch.setattr("src.notifier.telegram_delivery.AUTO_RETRY_DELAYS", (0.05,))
    return server


def channel(path, **kwargs):
    return TelegramChannel(bot_token="123:secret", channel_id="-10012345", cloudflare_url="https://proxy.test",
                           delivery_path=path, tz_offset_hours=3, max_transport_attempts=2, **kwargs)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("не дождались")


def rows(path, sql, *args):
    """Чтение базы, которую поток Telegram мог ещё не создать."""
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        try:
            return connection.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []


def test_unsent_post_is_delivered_in_background_once(server, tmp_path, caplog):
    path = tmp_path / "delivery.db"
    server.outcomes["sendPhoto"] = [unsent(), unsent(), unsent(), unsent()]
    instance = channel(path)
    with caplog.at_level(logging.INFO):
        instance.handle(demo_event("plan"))
        wait_for(lambda: server.sent("sendPhoto"))
        instance.close(5)

    assert len(server.sent("sendPhoto")) == 1
    assert rows(path, "SELECT status FROM operations WHERE operation_key LIKE '%:post'") == [("confirmed",)]
    assert rows(path, "SELECT root_id FROM trades WHERE root_id IS NOT NULL")
    history = rows(path, "SELECT source,status,detail FROM attempt_history ORDER BY attempt_id")
    assert history == [("initial", "deferred", "ConnectionError/NewConnectionError"),
                       ("automatic", "deferred", "ConnectionError/NewConnectionError"),
                       ("automatic", "confirmed", None)]
    assert rows(path, "SELECT COUNT(*) FROM telegram_auto_retry") == [(0,)]
    assert "secret" not in caplog.text and "proxy.test" not in caplog.text
    assert "запрос не ушёл" in caplog.text


def test_trade_waits_behind_deferred_post_and_other_trades_do_not(server, tmp_path, monkeypatch):
    monkeypatch.setattr("src.notifier.telegram_delivery.AUTO_RETRY_DELAYS", (0.5,))
    path = tmp_path / "delivery.db"
    server.outcomes["sendPhoto"] = [unsent(), unsent()]
    instance = channel(path)
    instance.handle(demo_event("plan"))
    instance.handle(demo_event("stop"))
    instance.handle(demo_event("plan", side="SELL", trade_id="other"))
    wait_for(lambda: len(server.sent("sendPhoto")) == 3)
    instance.close(5)

    other, root, stop = server.sent("sendPhoto")
    saved = dict(rows(path, "SELECT trade_id,data_json FROM operations WHERE operation_key LIKE '%:post' ORDER BY operation_id"))
    # Другая сделка ушла сразу, корень demo — после досылки, перенос стопа — за ним.
    assert other["data"]["caption"] == json.loads(saved["other"])["caption"]
    assert root["data"]["caption"] == json.loads(rows(path, "SELECT data_json FROM operations WHERE trade_id='demo' ORDER BY operation_id")[0][0])["caption"]
    root_id = server.calls.index(root) + 1
    assert f"https://t.me/test_channel/{root_id}" in stop["data"]["reply_markup"]
    assert rows(path, "SELECT root_id FROM trades WHERE trade_id='demo'") == [(root_id,)]


def test_deferred_resend_uses_saved_material(server, tmp_path):
    path = tmp_path / "delivery.db"
    server.outcomes["sendPhoto"] = [unsent(), unsent()]
    instance = channel(path)
    instance.handle(demo_event("plan"))
    wait_for(lambda: server.sent("sendPhoto"))
    instance.close(5)

    (call,) = server.sent("sendPhoto")
    saved = rows(path, "SELECT o.data_json,f.content FROM operations o JOIN telegram_files f USING(file_id)")[0]
    assert {key: call["data"][key] for key in json.loads(saved[0])} == json.loads(saved[0])
    assert call["photo"] == saved[1]


def test_mixed_uncertain_and_unsent_is_never_resent(server, tmp_path):
    path = tmp_path / "delivery.db"
    server.outcomes["sendPhoto"] = [read_timeout(), unsent()]
    instance = channel(path)
    instance.handle(demo_event("plan"))
    instance.handle(demo_event("plan", trade_id="other"))
    wait_for(lambda: server.sent("sendPhoto"))
    time.sleep(0.3)
    instance.close(5)

    assert len(server.sent("sendPhoto")) == 1
    statuses = rows(path, "SELECT trade_id,status FROM operations WHERE operation_key LIKE '%:post' ORDER BY operation_id")
    assert statuses == [("demo", "uncertain"), ("other", "confirmed")]


def test_unsent_root_edit_is_resent_without_duplicate_post(server, tmp_path):
    path = tmp_path / "delivery.db"
    server.outcomes["editMessageCaption"] = [unsent(), unsent()]
    instance = channel(path)
    instance.handle(demo_event("plan"))
    instance.handle(demo_event("entry"))
    wait_for(lambda: server.sent("editMessageCaption"))
    instance.close(5)

    # Пост входа (текст) отправлен один раз, досылается только правка корня.
    assert len(server.sent("sendPhoto")) == 1
    assert len(server.sent("sendMessage")) == 1
    assert len(server.sent("editMessageCaption")) == 1
    assert rows(path, "SELECT status FROM operations WHERE operation_key LIKE '%:root'") == [("confirmed",)]


def test_window_expiry_fails_operation_and_releases_trade(server, tmp_path, monkeypatch, caplog):
    # Окно с запасом над задержкой 0,05 с: на медленном раннере CI досылка успевает до его конца.
    monkeypatch.setattr("src.notifier.telegram_delivery.AUTO_RETRY_WINDOW", timedelta(seconds=1))
    path = tmp_path / "delivery.db"
    server.down = True
    instance = channel(path)
    instance.handle(demo_event("plan"))
    instance.handle(demo_event("entry"))
    wait_for(lambda: rows(path, "SELECT COUNT(*) FROM operations WHERE status='failed'") == [(2,)])
    instance.close(5)

    history = rows(path, "SELECT status,detail FROM attempt_history WHERE operation_id=1 ORDER BY attempt_id")
    assert history[-1][0] == "failed" and AUTO_RETRY_EXPIRED in history[-1][1]
    assert len(history) > 2
    assert "secret" not in caplog.text and "proxy.test" not in caplog.text


def test_restart_resumes_deferred_and_parked_but_not_uncertain(server, tmp_path, monkeypatch):
    path = tmp_path / "delivery.db"
    server.down = True
    instance = channel(path)
    instance.handle(demo_event("plan"))
    instance.handle(demo_event("stop"))
    wait_for(lambda: rows(path, "SELECT status FROM operations") == [("deferred",)])
    instance.close(5)

    # Перенос стопа ждал за корнем: при остановке сохранён без HTTP.
    assert rows(path, "SELECT status FROM operations ORDER BY operation_id") == [("deferred",), ("deferred",)]
    assert rows(path, "SELECT COUNT(*) FROM attempt_history WHERE operation_id=2") == [(0,)]
    repo = DeliveryRepository(path, namespace("123:secret", "-10012345", path))
    stale = repo.save_operation("old:post", "sendMessage", {"text": "старое"}, trade_id="old", event_key="x")
    repo.begin_operation(stale)
    repo.finish_operation(stale, ApiResult(False, uncertain=True))
    repo.close()

    server.down = False
    instance = channel(path)
    wait_for(lambda: len(server.calls) >= 2)
    instance.close(5)

    assert [call["method"] for call in server.calls] == ["sendPhoto", "sendPhoto"]
    assert not any(call["data"].get("text") == "старое" for call in server.calls)
    assert rows(path, "SELECT status FROM operations ORDER BY operation_id") == [("confirmed",), ("confirmed",), ("uncertain",)]
    assert rows(path, "SELECT root_id FROM trades WHERE trade_id='demo'") == [(1,)]
    assert rows(path, "SELECT role,message_id FROM stages") == [("СТОП", 2)]


def test_restart_after_window_does_not_send(server, tmp_path, monkeypatch):
    path = tmp_path / "delivery.db"
    server.down = True
    instance = channel(path)
    instance.handle(demo_event("plan"))
    wait_for(lambda: rows(path, "SELECT status FROM operations") == [("deferred",)])
    instance.close(5)

    monkeypatch.setattr("src.notifier.telegram_delivery.AUTO_RETRY_WINDOW", timedelta(0))
    server.down = False
    instance = channel(path)
    instance.handle(demo_event("stop"))
    wait_for(lambda: server.sent("sendPhoto"))
    instance.close(5)

    plan = json.loads(rows(path, "SELECT data_json FROM operations ORDER BY operation_id")[0][0])
    (post,) = server.sent("sendPhoto")
    assert post["data"]["caption"] != plan["caption"]
    assert rows(path, "SELECT status FROM operations ORDER BY operation_id")[0] == ("failed",)
    detail = rows(path, "SELECT detail FROM attempt_history WHERE operation_id=1 ORDER BY attempt_id DESC LIMIT 1")
    assert detail == [(AUTO_RETRY_EXPIRED,)]


def test_chat_metadata_is_requested_again_after_network_failure(server, tmp_path, monkeypatch):
    monkeypatch.setattr("src.notifier.telegram.CHAT_RETRY_DELAY", 0.0)
    server.outcomes["getChat"] = [unsent(), unsent()]
    path = tmp_path / "delivery.db"
    instance = channel(path)
    for stage in ("plan", "entry", "tp1"):
        instance.handle(demo_event(stage, candles=False))
    instance.close(10)

    posts = server.sent("sendMessage")
    assert "https://t.me/test_channel/" in posts[-1]["data"]["reply_markup"]
