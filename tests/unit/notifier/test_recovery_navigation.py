import json
from types import SimpleNamespace

import pytest
import requests

from src.notifier.telegram_delivery import DeliveryRepository, namespace
from src.notifier.telegram_navigation import descriptor, event_navigation
from src.notifier.telegram_recovery import list_pending, retry
from src.notifier.telegram_transport import ApiResult
from tests.support.telegram import demo_event
from tests.unit.notifier.test_visual_delivery import Server


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = SimpleNamespace(TELEGRAM_BOT_TOKEN="123:secret", TELEGRAM_CHANNEL_ID="-10012345",
                             CLOUDFLARE_URL="https://proxy.test", NOTIFIER_TELEGRAM_REQUEST_TIMEOUT=1,
                             BAR_TIME_TZ_OFFSET_HOURS=3)
    path = tmp_path / "telegram_delivery.sqlite3"
    repo = DeliveryRepository(path, namespace(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHANNEL_ID, path))
    server = Server()
    monkeypatch.setattr(requests, "post", server.post)
    yield config, repo, server, tmp_path
    repo.close()


def save(repo, event, *, message_id=None, legacy=False, trade="trade"):
    key = f"signal:{trade}" if event == "plan" else f"{trade}:pv:tp:tp-2:stamp:fill" if event == "final" else event
    captions = {"plan": "<b>BR</b>\n<b>В работе, ждёт подтверждения</b>",
                "tp1": "<b>BR</b>\n<b>ЦЕЛЬ1 · подтверждено</b>",
                "final": "<b>BR</b>\n<b>Сделка закрыта · остаток 0</b>"}
    op = repo.save_operation(f"{trade}:{key}:post", "sendPhoto", {"caption": captions[event], "reply_markup": '{"inline_keyboard":[]}'},
                             b"saved-png", trade_id=trade, event_key=key,
                             navigation=None if legacy else event_navigation(demo_event(event)))
    repo.begin_operation(op)
    repo.finish_operation(op, ApiResult(True, {"message_id": message_id}) if message_id else ApiResult(False, uncertain=True))
    return op


@pytest.mark.parametrize("private", [False, True])
def test_confirmed_v2_root_repairs_existing_posts_without_resending_even_after_cleanup(setup, private):
    config, repo, server, path = setup
    root = save(repo, "plan", message_id=742, legacy=True)
    save(repo, "tp1", message_id=740, legacy=True)
    save(repo, "final", legacy=True)  # Неподтверждённый итог не должен получить кнопку.
    save(repo, "plan", message_id=900, trade="other")
    with repo.connection:
        repo.connection.execute("UPDATE telegram_files SET created_at='2000-01-01T00:00:00+00:00'")
        repo.connection.execute("DROP TABLE operation_navigation")
        repo.connection.execute("PRAGMA user_version=2")
    repo.cleanup_files()
    if private:
        server.chat.pop("username")
    assert retry(config, path, operation_id=root) == 0
    assert repo.root("trade")[0] == 742
    assert dict(repo.stages("trade")) == {"ЦЕЛЬ1": 740}
    assert not any(c["method"].startswith("send") for c in server.calls)
    edits = {c["data"]["message_id"]: json.loads(c["data"]["reply_markup"])["inline_keyboard"]
             for c in server.calls if c["method"] == "editMessageReplyMarkup"}
    prefix = "https://t.me/c/12345" if private else "https://t.me/test_channel"
    assert edits == {742: [[{"text": "ЦЕЛЬ1", "url": prefix + "/740"}]],
                     740: [[{"text": "↗ Открыть сделку", "url": prefix + "/742"}]]}
    repo.update_root_text("trade", "Более новая подпись")
    server.calls.clear()
    assert retry(config, path, operation_id=root) == 0
    assert repo.root("trade")[2] == "Более новая подпись"
    assert not any(c["method"] in {"sendPhoto", "sendMessage", "editMessageCaption", "editMessageText"} for c in server.calls)


def test_retry_stage_includes_root_then_restores_navigation_and_persists_receipts(setup):
    config, repo, server, path = setup
    root = save(repo, "plan")
    save(repo, "tp1", message_id=740)
    final = save(repo, "final")
    assert retry(config, path, operation_id=final, include_uncertain=True) == 0
    sends = [c for c in server.calls if c["method"] == "sendPhoto"]
    assert len(sends) == 2
    assert all(c["photo"] == b"saved-png" for c in sends)
    assert repo.operation_material(root)["message_id"] == 1
    assert repo.operation_material(final)["message_id"] == 2
    assert repo.root("trade")[0] == 1
    assert dict(repo.stages("trade")) == {"ЦЕЛЬ1": 740, "Итог": 2, "ЦЕЛЬ2": 2}
    edits = [c for c in server.calls if c["method"] == "editMessageReplyMarkup"]
    root_markup = json.loads(next(c["data"]["reply_markup"] for c in edits if c["data"]["message_id"] == 1))
    assert {b["text"] for row in root_markup["inline_keyboard"] for b in row} == {"ЦЕЛЬ1", "ЦЕЛЬ2", "Итог"}
    server.calls.clear()
    assert retry(config, path, operation_id=final) == 0
    assert not any(c["method"].startswith("send") for c in server.calls)


@pytest.mark.parametrize("allow", [False, True])
def test_missing_root_blocks_stage_on_denied_uncertain_or_timeout(setup, allow):
    config, repo, server, path = setup
    save(repo, "plan")
    final = save(repo, "final")
    server.fail["sendPhoto"] = requests.Timeout()
    assert retry(config, path, operation_id=final, include_uncertain=allow) == 1
    assert len(server.calls) == (1 if allow else 0)
    assert repo.root("trade") is None
    assert repo.operation_material(final)["status"] == "uncertain"


def test_failed_keyboard_edit_is_recoverable_without_duplicate_publication(setup):
    config, repo, server, path = setup
    root = save(repo, "plan", message_id=742)
    save(repo, "tp1", message_id=740)
    server.fail["editMessageReplyMarkup"] = requests.Timeout()
    assert retry(config, path, operation_id=root) == 1
    assert repo.operation_material(root)["status"] == "confirmed"
    failures = [r for r in repo.pending() if r["event_key"] == "navigation"]
    assert failures
    server.fail.clear()
    server.calls.clear()
    assert retry(config, path, operation_id=failures[0]["operation_id"]) == 0
    assert not any(c["method"].startswith("send") for c in server.calls)
    assert not repo.pending()
    history = repo.connection.execute("SELECT status FROM attempt_history WHERE operation_id=?", (failures[0]["operation_id"],)).fetchone()
    assert history[0] == "uncertain"  # История исходной неудачи не переписана.


def test_missing_channel_metadata_reports_incomplete_navigation(setup):
    config, repo, server, path = setup
    root = save(repo, "plan", message_id=742)
    server.fail["getChat"] = requests.Timeout()
    assert retry(config, path, operation_id=root) == 1
    assert [c["method"] for c in server.calls] == ["getChat"]


def test_pending_list_stays_read_only_and_excludes_confirmed(setup, capsys):
    config, repo, server, path = setup
    save(repo, "plan", message_id=742)
    assert list_pending(config, path) == 0
    assert "Неподтверждённых уведомлений Telegram нет" in capsys.readouterr().out
    assert not server.calls


def test_legacy_final_roles_use_only_known_header_and_target_event_key(setup):
    _, repo, _, _ = setup
    save(repo, "final", legacy=True)
    op = repo.trade_operations("trade")[0]
    assert descriptor(op, "trade")["roles"] == ["Итог", "ЦЕЛЬ2"]
    op["data_json"] = json.dumps({"caption": "Произвольная подпись с ЦЕЛЬ9 и итогом"})
    assert descriptor(op, "trade")["roles"] == []
