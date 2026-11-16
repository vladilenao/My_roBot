"""Строка жизни заявки контура на брокере (add-tinvest-orders) — общая для Telegram и консоли.

Текст простой, без разметки: Telegram экранирует его сам.
"""

from __future__ import annotations

from decimal import Decimal

from src.events.types import EventType

ORDER_KINDS = {"entry": "вход", "add": "добор", "target": "цель", "stop": "стоп", "exit": "выход",
               "emergency": "аварийный выход", "broker_stop": "стоп у брокера"}
# Почему стоп-заявка пересоздана (add-broker-protection).
STOP_REASONS = {"entry": "вход", "add": "добор", "target": "цель", "reduce": "сокращение", "move": "перенос",
                "restart": "рестарт", "replace": "пересоздание"}


def _price(value) -> str:
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _lots(number) -> str:
    number = int(number)
    forms = ("лот", "лота", "лотов")
    form = forms[0] if number % 10 == 1 and number % 100 != 11 else (
        forms[1] if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14) else forms[2])
    return f"{number} {form}"


def order_line(event) -> str:
    """``broker_order`` или ``order_accepted`` одной строкой; номер заявки — последние четыре символа."""
    tail = event.get("broker_order") or (event.get("order_id") if event.type is EventType.ORDER_ACCEPTED else None)
    number = f", заявка …{str(tail)[-4:]}" if tail else ""
    if event.type is EventType.ORDER_ACCEPTED:
        return f"принято брокером{number}"
    kind = event.get("kind")
    if kind in ("stop_placed", "stop_triggered", "stop_failed", "stop_removed", "stop_duplicate_cancelled"):
        return _stop_line(event, kind)
    what = ORDER_KINDS.get(event.get("order"), event.get("order") or "заявка")
    lots = f" · {_lots(event.get('lots'))}" if event.get("lots") else ""
    at = f" {_price(event.get('price'))}" if event.get("price") is not None else ""
    reason = f": {event.get('reason')}" if event.get("reason") else ""
    if kind == "sent":
        how = "по рынку" if event.get("order_type") == "market" else f"лимит{at}"
        return f"отправлено в Т-Инвест: {what}, {how}{lots}"
    if kind == "unknown":
        return f"ответ брокера потерян ({what}), ищем заявку по ключу"
    if kind == "found":
        state = f", {event.get('state')}" if event.get("state") else ""
        return f"заявка найдена по ключу ({what}{state}){number}"
    if kind == "resent":
        return f"по ключу пусто, {what} отправлен снова{lots}"
    if kind == "cancelling":
        return f"снимается: {what}{number}"
    if kind == "requoted":
        return f"{what} перевыставлен, попытка {event.get('attempt')}: лимит{at}{lots}"
    if kind == "exit_rejected":
        return f"⚠️ брокер отклонил: {what}{reason}; повтор не раньше чем через минуту"
    if kind == "emergency_market":
        return f"⚠️ {what}: лимит не исполнился, выход по рынку{lots}"
    return f"{what}: {kind}"


def _stop_line(event, kind: str) -> str:
    """Строка стоп-заявки у брокера: номер — последние четыре символа стоп-заявки."""
    tail = event.get("broker_order")
    number = f", стоп-заявка …{str(tail)[-4:]}" if tail else ""
    lots = _lots(event.get("lots")) if event.get("lots") else ""
    at = _price(event.get("price")) if event.get("price") is not None else ""
    if kind == "stop_placed":
        if not event.get("replaced"):
            return f"стоп у брокера {at} · {lots}{number}"
        if event.get("reason") == "move":
            return f"стоп перенесён: {at} · {lots}{number}"
        why = STOP_REASONS.get(event.get("reason"), event.get("reason") or "")
        return f"стоп пересоздан на остаток {lots}" + (f" ({why})" if why else "") + f": {at}{number}"
    if kind == "stop_triggered":
        exchange = event.get("exchange_order")
        return "стоп сработал у брокера" + (f", заявка …{str(exchange)[-4:]}" if exchange else "")
    if kind == "stop_failed":
        reason = event.get("reason")
        return "⚠️ стоп не принят брокером" + (f": {reason}" if reason else "") + " — выход из сделки"
    if kind == "stop_duplicate_cancelled":
        return f"лишняя стоп-заявка снята{number}: ответ на её постановку был потерян"
    return f"стоп снят перед выходом{number}"
