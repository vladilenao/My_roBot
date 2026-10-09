"""Русское представление Telegram. Консольные шаблоны сюда не обращаются."""
from decimal import Decimal
from html import escape
from datetime import timedelta

from src.events.types import EventType
from src.events.visual import utc

PHASES = {"PLANNED": "План создан", "ENTRY_PENDING": "В работе, ждёт подтверждения",
          "OPEN": "Открыта", "BUILDING": "Открыта · добор", "REDUCING": "Частично закрыта",
          "PARTIALLY_CLOSED": "Частично закрыта", "CLOSED": "Закрыта ✅", "CANCELLED": "Отменена",
          "REJECTED": "Вход отклонён", "ERROR": "Ошибка сопровождения"}
ROLES = {"OPEN": "Вход", "ADD": "Добор", "STOP": "СТОП", "TARGET": "Цель", "CLOSE": "Выход", "REDUCE": "Частичный выход"}
CONSTRAINTS = {"risk": "риск", "margin": "ГО", "max-quantity": "предел количества",
               "requested-quantity": "явный запрос", "profile": "профиль"}


def price(value, step=None):
    if value is None:
        return "неизвестно"
    value = Decimal(str(value))
    if step:
        precision = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)
        return f"{value:.{precision}f}"
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def money(value, *, signed=False, units="RUB"):
    if value is None:
        return "неизвестно"
    amount = price(value)
    if signed and Decimal(str(value)) > 0:
        amount = "+" + amount
    return amount + (" ₽" if units == "RUB" else " в единицах цены")


def quantity(number, unit="contract"):
    number = int(number)
    forms = ("лот", "лота", "лотов") if unit == "lot" else ("контракт", "контракта", "контрактов")
    form = forms[2] if 11 <= number % 100 <= 14 else forms[0] if number % 10 == 1 else forms[1] if 2 <= number % 10 <= 4 else forms[2]
    return f"{number} {form}"


def timeframe_label(value):
    if not value:
        return ""
    return value[:-1] + {"m": "м", "h": "ч", "d": "д", "w": "н", "M": "мес"}.get(value[-1], value[-1])


def terminal(event):
    visual = event.get("visual")
    return bool(visual and visual["state"]["phase"] == "CLOSED" and visual["state"]["quantity"] == 0)


def event_role(event):
    visual = event.get("visual")
    if terminal(event):
        return "Итог"
    if event.type is EventType.STOP_MOVED:
        return "СТОП"
    if visual:
        key = visual["event_key"]
        fill = next((f for f in visual["fills"] if f["key"] == key), None)
        if fill:
            return ROLES.get(fill["role"], fill["role"])
    return {EventType.SIGNAL: "План", EventType.TRADE_OPENED: "Вход", EventType.POSITION_ADDED: "Добор",
            EventType.STOP_HIT: "СТОП", EventType.TARGET_HIT: "Цель", EventType.TRADE_CLOSED: "Выход",
            EventType.TRADE_CANCELLED: "Отмена заявки", EventType.ORDER_REJECTED: "Заявка отклонена"}.get(event.type, "Событие")


def _levels(data, step, unit, *, current=False):
    lines = []
    if "stop" in data:
        lines.append(f"СТОП: <code>{price(data['stop'], step)}</code>")
    for target in data.get("targets", ()):
        remaining = target["quantity"] - target.get("filled", 0) if current else target["quantity"]
        if remaining > 0:
            lines.append(f"ЦЕЛЬ{target['number']}: <code>{price(target['price'], step)}</code> — {quantity(remaining, unit)}")
    if not data.get("targets"):
        lines.append("Цели: нет")
    if data.get("trailing_quantity", 0):
        lines.append("Остаток сопровождается стопом")
    return lines


def _financial(financial, *, final=True):
    lines = []
    units = financial.get("units", "RAW")
    if financial.get("gross") is not None:
        lines.append("Результат до комиссии: <b>" + money(financial["gross"], signed=True, units=units) + "</b>")
    source = financial.get("fees_source", "unknown")
    known = financial.get("fees_known", False)
    if not known:
        lines.append("Комиссии: неполные данные")
    else:
        label = "Комиссии" if source == "broker" else "Комиссии (оценка)" if source == "configured" else "Комиссии (факты и оценки)"
        lines.append(label + ": <b>" + money(-Decimal(str(financial["fees"]))) + "</b>")
    if financial.get("net") is not None and known and units == "RUB":
        label = "Итог после комиссий" if final else "Получено после комиссий"
        if source != "broker":
            label += " (с оценкой)"
        lines.append(f"<b>{label}: {money(financial['net'], signed=True)}</b>")
    elif units == "RAW":
        lines.append("Рублёвый итог недоступен: нет денежного фактора")
    else:
        lines.append("Итог после комиссий неизвестен")
    return lines


def _plan_financial(plan):
    lines = []
    for field, label in (("risk_amount", "Риск до стопа"), ("costs_amount", "Расходы ≈"),
                         ("reward_amount", "Плановая прибыль до расходов"), ("net_reward_amount", "Плановая прибыль после расходов")):
        if plan.get(field) is not None:
            lines.append(f"{label}: <b>{money(plan[field])}</b>")
    if plan.get("reward_amount") is None and plan.get("fixed_reward_amount") is not None:
        lines.append("Фиксируемая часть до расходов: " + money(plan["fixed_reward_amount"]))
        lines.append("Полная плановая прибыль неизвестна")
    if plan.get("requested_quantity") is not None and plan["requested_quantity"] != plan["quantity"]:
        lines.append(f"Запрошено {plan['requested_quantity']}, выбрано {plan['quantity']}")
    if plan.get("limiting_constraint"):
        lines.append("Ограничение: " + escape(CONSTRAINTS.get(plan["limiting_constraint"], "условия допуска")))
    if plan.get("algorithm_version"):
        lines.append("Версия: " + escape(plan["algorithm_version"]))
    return lines


def local_time(value, offset=0):
    moment = utc(value) + timedelta(hours=offset)
    return f"{moment:%d.%m %H:%M} UTC{offset:+g}"


def _history(visual, step, unit, offset):
    facts = []
    for fill in visual["fills"]:
        label = ROLES.get(fill["role"], fill["role"])
        facts.append((fill['time'], f"{escape(label)}: {quantity(fill['quantity'], unit)} × {price(fill['price'], step)} · {local_time(fill['time'], offset)}"))
    for stop in visual["stops"]:
        if stop["reason"] != "entry-protection":
            facts.append((stop["time"], f"СТОП перенесён: {price(stop.get('old'), step)} → {price(stop['new'], step)} · {local_time(stop['time'], offset)}"))
    lines = [text for _, text in sorted(facts, key=lambda item: utc(item[0]))]
    for group in visual["fill_groups"]:
        label = ROLES.get(group["role"], group["role"])
        lines.append(f"Ранние {escape(label)} (сводка {group['count']} исполнений): {quantity(group['quantity'], unit)} · "
                     f"средняя {price(group['price'], step)} · {local_time(group['start'], offset)} — {local_time(group['end'], offset)}")
    return lines


def render(event, *, root=False, tz_offset_hours=0):
    visual = event.get("visual")
    instrument = escape(event.instrument or "контракт не указан")
    if not visual:
        role = event_role(event)
        side = "ПОКУПКА" if event.get("side") == "BUY" else "ПРОДАЖА" if event.get("side") == "SELL" else ""
        lines = [f"<b>{instrument} · {escape(role)} {side}</b>"]
        if event.type is EventType.SIGNAL:
            lines += ["В работе, ждёт подтверждения", f"Вход: {price(event.get('entry'))} · {quantity(event.get('quantity', 0))}",
                      f"СТОП: {price(event.get('stop'))}"]
            lines += [f"ЦЕЛЬ{i}: {price(target)} · объём неизвестен" for i, target in enumerate(event.get("targets", ()), 1)]
            lines += _plan_financial(event.payload)
        else:
            if event.get("price") is not None:
                lines.append(f"Исполнено: {quantity(event.get('quantity', 0))} по {price(event.get('price'))}")
            if event.get("gross_pnl") is not None:
                lines += _financial({"gross": event.get("gross_pnl"), "net": event.get("net_pnl"),
                                     "fees": event.get("fees_total"), "fees_known": event.get("fees_known", False),
                                     "fees_source": event.get("fee_source", "unknown"), "units": event.get("pnl_units", "RAW")})
        return "\n".join(lines)
    plan, state = visual["plan"], visual["state"]
    step, unit = visual.get("price_step"), visual["unit"]
    side = "ПОКУПКА" if visual["side"] == "BUY" else "ПРОДАЖА"
    role = event_role(event)
    lines = [f"<b>{instrument} · {side} · {timeframe_label(visual['timeframe'])}</b>"]
    if root or event.type is EventType.SIGNAL:
        status = PHASES.get(state['phase'], 'Статус неизвестен')
        entered = sum(f['quantity'] for f in visual['fills'] if f['role'] == 'OPEN') + sum(g['quantity'] for g in visual['fill_groups'] if g['role'] == 'OPEN')
        if state['phase'] in {'OPEN', 'BUILDING'} and 0 < entered < plan['quantity']:
            status += " · вход исполнен частично"
        lines += [f"<b>{status}</b>", "", "Первоначальный план:",
                  f"Вход: <code>{price(plan['entry'], step)}</code> · <b>{quantity(plan['quantity'], unit)}</b>"]
        if root and state["phase"] != "ENTRY_PENDING":
            lines.insert(2, "Текущий остаток: " + quantity(state["quantity"], unit))
            if state.get("stop") is not None and state["quantity"]:
                lines.insert(3, "Действующий СТОП: " + price(state["stop"], step))
            if state["phase"] == "CLOSED":
                lines[3:3] = _financial(visual["financial"])
        lines += _levels(plan, step, unit)
        if event.type is EventType.SIGNAL and not root:
            lines += [""] + _plan_financial(plan)
        if state["phase"] != "ENTRY_PENDING":
            lines += ["", "Текущий остаток: " + quantity(state["quantity"], unit)]
            if state.get("average_entry") is not None:
                lines.append("Средняя входа: " + price(state["average_entry"], step))
            if state.get("stop") is not None and state["quantity"]:
                lines.append("Действующий СТОП: " + price(state["stop"], step))
            history = [(f["time"], ROLES.get(f["role"], f["role"])) for f in visual["fills"] if f["role"] not in {"OPEN", "ADD"}]
            history += [(s["time"], "СТОП перенесён") for s in visual["stops"] if s["reason"] != "entry-protection"]
            roles = [role for _, role in sorted(history, key=lambda item: utc(item[0]))]
            if roles:
                lines.append("История: " + " → ".join(escape(r) for r in roles[-5:]))
        if state["phase"] == "CLOSED" and not root:
            lines += [""] + _financial(visual["financial"])
    elif role == "Итог":
        lines += ["<b>Сделка закрыта · остаток 0</b>", ""] + _financial(visual["financial"])
        lines += ["", "Как прошла сделка:"] + _history(visual, step, unit, tz_offset_hours)
    elif event.type is EventType.STOP_MOVED:
        stop = next((s for s in visual["stops"] if s["key"] == visual["event_key"]), None)
        lines += ["<b>СТОП перенесён и подтверждён</b>", ""]
        if stop:
            lines += ["Прежний СТОП: " + price(stop.get("old"), step), "Новый СТОП: <b>" + price(stop["new"], step) + "</b>"]
            if stop["reason"] == "cost-aware-break-even":
                lines.append("Новый СТОП учитывает уже полученную прибыль по целям и расходы всей сделки.")
        lines.append("Остаток: " + quantity(state["quantity"], unit))
        lines += _levels({"targets": state["targets"]}, step, unit, current=True)
        lines.append("Прежний уровень на графике — серый пунктир.")
    elif event.type in {EventType.TRADE_CANCELLED, EventType.ORDER_REJECTED}:
        lines += [f"<b>{escape(role)}</b>", "Статус сделки: " + PHASES.get(state["phase"], "неизвестен"),
                  "Остаток: " + quantity(state["quantity"], unit)]
        if state["quantity"]:
            lines.append("Открытая позиция сохранена; отмена/отказ относится к заявке.")
    else:
        fill = next((f for f in visual["fills"] if f["key"] == visual["event_key"]), None)
        partial = event.type in {EventType.TRADE_OPENED, EventType.POSITION_ADDED} and event.get("status") == "partial"
        if fill and fill["role"].startswith("ЦЕЛЬ"):
            target = next((t for t in state["targets"] if "ЦЕЛЬ" + str(t["number"]) == fill["role"]), None)
            partial = bool(target and target.get("filled", 0) < target["quantity"])
        lines += [f"<b>{escape(role)} · {'частично исполнена' if partial else 'подтверждено'}</b>"]
        if fill:
            verb = "Куплено" if fill["side"] == "BUY" else "Продано"
            lines.append(f"{verb}: {quantity(fill['quantity'], unit)} по <code>{price(fill['price'], step)}</code>")
        if state.get("average_entry") is not None:
            lines.append("Средняя входа: " + price(state["average_entry"], step))
        lines += ["Остаток: " + quantity(state["quantity"], unit)] + _levels(state, step, unit, current=True)
        lines += [""] + _financial(visual["financial"], final=False)
    if visual["market"]["limited"] or visual["market"]["gaps"] or visual["market"]["history_limited"] or visual["market"]["stop_history_limited"]:
        lines.append("<i>График: ограниченное окно / неполная история; пропуски не дорисованы.</i>")
    return "\n".join(lines)
