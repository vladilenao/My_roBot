"""Кнопка и причина частичного выхода (change telegram-partial-exit-navigation)."""

import json
from datetime import datetime
from decimal import Decimal

from src.events.event import Event
from src.events.types import EventType
from src.events.visual import VisualSnapshot, serializable
from src.notifier.telegram_html import visible_text
from src.notifier.telegram_navigation import event_navigation, register_confirmed
from src.notifier.telegram_recovery import retry
from src.notifier.telegram_templates import render
from src.notifier.telegram_transport import ApiResult
from tests.support.telegram import demo_event
from tests.unit.notifier.test_recovery_navigation import setup  # noqa: F401 — фикстура восстановления
from tests.unit.notifier.test_visual_delivery import channel, server  # noqa: F401 — фикстура транспорта

REDUCE_TITLE = "Частичный выход · подтверждено"
QUANTITY = 6


def reduce_event(key="reduce", *, reason="ma10-or-cloud-partial-exit", quantity=1, prior=0, closed=False):
    """Частичный выход после ЦЕЛЬ1 и переноса стопа в демо-сделке на 6 лотов (остаток 3).

    ``prior`` — сколько выходов по одному лоту уже подтверждено раньше.
    """
    data = serializable(demo_event("stop", quantity=QUANTITY).get("visual"))
    exits = [{"key": f"reduce-{i}", "time": datetime(2026, 10, 6, 8, 30 + i).isoformat(), "role": "REDUCE",
              "side": "SELL", "quantity": 1, "price": "302.10"} for i in range(prior)]
    fill = {"key": key, "time": datetime(2026, 10, 6, 8, 30 + prior).isoformat(), "role": "REDUCE", "side": "SELL",
            "quantity": quantity, "price": "302.10", **({"reason": reason} if reason is not None else {})}
    remaining = data["state"]["quantity"] - prior - quantity
    data.update(event_key=key, fills=[*data["fills"], *exits, fill],
                state=dict(data["state"], quantity=remaining, phase="CLOSED" if closed else "REDUCING"))
    return Event.broker_event(EventType.TRADE_CLOSED, trade_id="demo", instrument="SBER", timeframe="15m",
                              bar_time=datetime(2026, 10, 6, 8, 30 + prior), quantity=quantity,
                              price=Decimal("302.10"), status="fill", visual=VisualSnapshot(data))


def _posts(transport, title):
    return [index + 1 for index, call in enumerate(transport.calls)
            if call["method"] in {"sendPhoto", "sendMessage"}
            and title in (call["data"].get("caption") or call["data"].get("text") or "")]


def _root_buttons(transport):
    edit = [c for c in transport.calls if c["method"] in {"editMessageCaption", "editMessageText"}][-1]
    return {b["text"]: b["url"] for row in json.loads(edit["data"]["reply_markup"])["inline_keyboard"] for b in row}


class TestDelivery:
    def test_stop_and_partial_exit_on_one_bar_get_separate_buttons(self, server, tmp_path):  # noqa: F811
        instance = channel(tmp_path / "delivery.db")
        for stage in ("plan", "entry", "tp1", "stop"):
            instance.handle(demo_event(stage, quantity=QUANTITY))
        instance.handle(reduce_event())
        instance.close(10)

        (stop_post,) = _posts(server, "СТОП перенесён и подтверждён")
        (exit_post,) = _posts(server, REDUCE_TITLE)
        buttons = _root_buttons(server)
        assert buttons["СТОП"] == f"https://t.me/test_channel/{stop_post}"
        assert buttons["Частичный выход"] == f"https://t.me/test_channel/{exit_post}"

    def test_several_partial_exits_keep_one_button_on_the_last(self, server, tmp_path):  # noqa: F811
        instance = channel(tmp_path / "delivery.db")
        for stage in ("plan", "entry", "tp1", "stop"):
            instance.handle(demo_event(stage, quantity=QUANTITY))
        instance.handle(reduce_event("reduce-0"))
        instance.handle(reduce_event("reduce-1", prior=1))
        instance.close(10)

        posts = _posts(server, REDUCE_TITLE)
        assert len(posts) == 2
        buttons = _root_buttons(server)
        assert list(buttons).count("Частичный выход") == 1
        assert buttons["Частичный выход"] == f"https://t.me/test_channel/{posts[-1]}"

    def test_navigation_saved_before_http_names_the_role(self):
        assert event_navigation(reduce_event()) == {"root": False, "roles": ["Частичный выход"]}

    def test_terminal_reduce_stays_final(self):
        assert event_navigation(reduce_event(quantity=3, closed=True))["roles"] == ["Итог"]


class TestReason:
    def test_profile_reason_is_shown_in_russian(self):
        text = visible_text(render(reduce_event(), tz_offset_hours=3))

        assert REDUCE_TITLE in text
        assert "Продано: 1 лот по 302.10" in text
        assert "Причина: частичный выход по MA10/облаку" in text
        assert "ma10-or-cloud-partial-exit" not in text
        assert text.index("Продано:") < text.index("Причина:")

    def test_unknown_or_missing_reason_has_no_line(self):
        for reason in (None, "some-new-code"):
            text = visible_text(render(reduce_event(reason=reason), tz_offset_hours=3))
            assert REDUCE_TITLE in text and "Продано: 1 лот" in text
            assert "Причина:" not in text
            assert "some-new-code" not in text


def _save(repo, key, caption, navigation, message_id):
    op = repo.save_operation(f"trade:{key}:post", "sendPhoto", {"caption": caption, "reply_markup": '{"inline_keyboard":[]}'},
                             b"saved-png", trade_id="trade", event_key=key, navigation=navigation)
    repo.begin_operation(op)
    repo.finish_operation(op, ApiResult(True, {"message_id": message_id}))
    return op


class TestRecovery:
    def test_partial_exit_delivered_before_the_role_is_registered(self, setup):  # noqa: F811
        config, repo, transport, path = setup
        root = _save(repo, "signal:trade", "<b>ED</b>\n<b>В работе</b>", {"root": True, "roles": []}, 700)
        _save(repo, "stop", "<b>ED</b>\n<b>СТОП перенесён и подтверждён</b>", {"root": False, "roles": ["СТОП"]}, 792)
        exit_op = _save(repo, "reduce", f"<b>ED</b>\n<b>{REDUCE_TITLE}</b>", {"root": False, "roles": []}, 793)
        _save(repo, "tp1", "<b>ED</b>\n<b>ЦЕЛЬ1 · подтверждено</b>", {"root": False, "roles": []}, 794)

        assert retry(config, path, operation_id=root) == 0

        assert dict(repo.stages("trade")) == {"СТОП": 792, "Частичный выход": 793}
        assert not any(c["method"].startswith("send") for c in transport.calls)
        root_edit = next(c for c in transport.calls if c["method"] == "editMessageReplyMarkup" and c["data"]["message_id"] == 700)
        assert json.loads(root_edit["data"]["reply_markup"])["inline_keyboard"] == [[
            {"text": "СТОП", "url": "https://t.me/test_channel/792"},
            {"text": "Частичный выход", "url": "https://t.me/test_channel/793"}]]
        saved = repo.connection.execute("SELECT data_json FROM operation_navigation WHERE operation_id=?", (exit_op,)).fetchone()
        assert json.loads(saved[0]) == {"root": False, "roles": []}

    def test_register_confirmed_is_repeatable(self, setup):  # noqa: F811
        _, repo, _, _ = setup
        _save(repo, "reduce", f"<b>ED</b>\n<b>{REDUCE_TITLE}</b>", {"root": False, "roles": []}, 793)

        register_confirmed(repo, "trade")
        register_confirmed(repo, "trade")

        assert dict(repo.stages("trade")) == {"Частичный выход": 793}
