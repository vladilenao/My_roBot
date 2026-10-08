"""Восстановление ссылок только по сохранённым материалам и receipts."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from src.events.types import EventType
from src.notifier.telegram_delivery import markup
from src.notifier.telegram_templates import event_role


def event_navigation(event):
    role = event_role(event)
    roles = [role] if role.startswith("ЦЕЛЬ") or role in {"СТОП", "Итог"} else []
    visual = event.get("visual")
    if role == "Итог" and visual:
        fill = next((f for f in visual["fills"] if f["key"] == visual["event_key"]), None)
        if fill and fill["role"].startswith("ЦЕЛЬ"):
            roles.append(fill["role"])
    return {"root": event.type is EventType.SIGNAL, "roles": roles}


def descriptor(operation, trade_id):
    if operation["navigation_json"] is not None:
        return json.loads(operation["navigation_json"])
    # Совместимость с v2: только известные форматы основного поста.
    result = {"root": False, "roles": []}
    if operation["method"] not in {"sendPhoto", "sendMessage"} or not operation["operation_key"].endswith(":post"):
        return result
    result["root"] = operation["event_key"] == f"signal:{trade_id}"
    data = json.loads(operation["data_json"])
    lines = (data.get("caption") or data.get("text") or "").splitlines()
    title = lines[1] if len(lines) > 1 else ""
    target = re.fullmatch(r"<b>(ЦЕЛЬ\d+) · (?:подтверждено|частично исполнена)</b>", title)
    if target:
        result["roles"] = [target[1]]
    elif title == "<b>СТОП перенесён и подтверждён</b>":
        result["roles"] = ["СТОП"]
    elif title == "<b>Сделка закрыта · остаток 0</b>":
        result["roles"] = ["Итог"]
        target = re.search(r":pv:tp:tp-(\d+):", operation["event_key"])
        if target:
            result["roles"].append("ЦЕЛЬ" + target[1])
    return result


def register_confirmed(repo, trade_id):
    """Повторяемая локальная регистрация, без HTTP и без отката root_text."""
    posts = [op for op in repo.trade_operations(trade_id)
             if op["status"] == "confirmed" and op["message_id"]
             and op["method"] in {"sendPhoto", "sendMessage"}]
    for op in posts:
        nav = descriptor(op, trade_id)
        if nav["root"] and repo.root(trade_id) is None:
            data = json.loads(op["data_json"])
            repo.set_root(trade_id, op["message_id"], "photo" if op["method"] == "sendPhoto" else "text",
                          data.get("caption", data.get("text", "")))
        for role in nav["roles"]:
            repo.add_stage(trade_id, role, op["message_id"])
    return posts


def message_link(chat, message_id):
    if not chat or chat.get("type") not in {"channel", "supergroup"}:
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    identifier = str(chat.get("id", ""))
    if identifier.startswith("-100"):
        return f"https://t.me/c/{identifier[4:]}/{message_id}"
    return None


def repair_navigation(repo, transport, trade_id):
    posts = register_confirmed(repo, trade_id)
    if not posts:
        return True
    root = repo.root(trade_id)
    if root is None:
        print(f"Навигация {trade_id}: основная карточка не подтверждена или не распознана.")
        return False
    response = transport.request("getChat")
    if not response.ok or not isinstance(response.result, dict):
        print(f"Навигация {trade_id}: не удалось получить адрес канала; повторите команду.")
        return False
    chat = response.result
    root_link = message_link(chat, root[0])
    if root_link is None:
        print(f"Навигация {trade_id}: этот тип чата не поддерживает ссылки на сообщения.")
        return True
    confirmed_ids = {op["message_id"] for op in posts}
    root_buttons = [{"text": role, "url": message_link(chat, message_id)}
                    for role, message_id in repo.stages(trade_id) if message_id in confirmed_ids]
    ok = True
    for post in posts:
        buttons = root_buttons if post["message_id"] == root[0] else [{"text": "↗ Открыть сделку", "url": root_link}]
        data = {"message_id": post["message_id"], "reply_markup": markup(buttons)}
        # Редактирование клавиатуры идемпотентно: повтор не создаёт публикаций.
        # Каждая попытка фиксируется отдельно, даже если такой набор кнопок уже был.
        operation_id = repo.save_operation(f"navigation:{trade_id}:{post['message_id']}:{uuid4().hex}",
                                          "editMessageReplyMarkup", data, trade_id=trade_id, event_key="navigation")
        repo.begin_operation(operation_id, source="manual-navigation")
        response = transport.request("editMessageReplyMarkup", data)
        repo.finish_operation(operation_id, response)
        if response.ok:
            repo.supersede_navigation(trade_id, post["message_id"], operation_id)
        ok = response.ok and ok
    if not ok:
        print(f"Навигация {trade_id}: часть кнопок не обновлена; повторите команду, публикации не дублируются.")
    return ok
