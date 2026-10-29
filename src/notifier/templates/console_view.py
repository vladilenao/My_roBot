"""Пользовательская лента «События»: текстовая сетка и смысловые цвета."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates.holds import UNKNOWN_SCHEDULE, data_reason
from src.trade_journal.reasons import reason_label

COLORS = {"buy": "\x1b[32m", "sell": "\x1b[35m", "warning": "\x1b[33m",
          "error": "\x1b[31m", "muted": "\x1b[90m"}
RESET = "\x1b[0m"
# Консоль целиком по Москве: MOEX и FORTS живут в UTC+3 без летнего времени.
MOSCOW = timezone(timedelta(hours=3))
SIDES = {"BUY": "ПОКУПКА", "SELL": "ПРОДАЖА"}
CONSTRAINTS = {"risk": "риск", "margin": "ГО", "max-quantity": "предел количества",
               "requested-quantity": "явный запрос", "profile": "профиль"}
# Безубыток в консоли объясняется развёрнуто; остальные причины переноса —
# подписи общего словаря причин журнала.
STOP_MOVE_REASONS = {"cost-aware-break-even": "Безубыток всей сделки: учтены прибыль целей и расходы"}
# Пара «прежний → новый» склеивается до переноса строк и раскрывается после.
_GLUE = "\u00a0"
DATA_LABELS = {"warming_up": "ПРОГРЕВ", "coverage_blocked": "ДАННЫЕ НЕПОЛНЫЕ",
               "delayed": "ЗАДЕРЖКА ДАННЫХ", "recovered": "ДАННЫЕ ВОССТАНОВЛЕНЫ"}


def number(value: Any, *, places: int = 2) -> str:
    amount = Decimal(str(value))
    rounded = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    result = format(abs(rounded), f",.{places}f").replace(",", " ").replace(".", ",")
    if places == 2 and result.endswith(",00"):
        result = result[:-3]
    return ("−" if amount < 0 else "") + result


def money(value: Any, *, estimate: bool = False, units: str = "RUB", places: int = 2) -> str:
    if value is None:
        return "неизвестно"
    return f"{'~' if estimate else ''}{number(value, places=places)} {'₽' if units == 'RUB' else units}"


def price(value: Any, *, decision: bool = False) -> str:
    if value is None:
        return "неизвестно"
    amount = Decimal(str(value))
    if decision:
        return format(amount.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), ".3f")
    return format(amount, "f")


def target_price(value: Any) -> str:
    """Цена цели с разрядами исходного шага и минимум одним десятичным знаком."""
    if value is None:
        return "неизвестно"
    places = max(1, -Decimal(str(value)).as_tuple().exponent)
    return money(value, places=places)


def timeframe(value: str) -> str:
    if value.endswith("m"):
        return value[:-1] + "м"
    if value.endswith("h"):
        return value[:-1] + "ч"
    return value


def moscow(value: Any) -> datetime | None:
    """Момент по Москве; наивное значение — UTC, неразборчивое — None."""
    if value is None or value == "":
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW)


def headline(event: Event, now: datetime | None) -> datetime:
    """Момент заголовка: наблюдение, закрытие бара или публикация — по Москве."""
    if event.type is EventType.MARKET_DATA_STATUS:
        stamp = event.get("observed_at") or now
    else:
        stamp = event.bar_time or now
    return moscow(stamp) or datetime.now(MOSCOW)


def when(event: Event, now: datetime | None) -> str:
    return headline(event, now).strftime("%H:%M")


def clock(value: Any, anchor: datetime) -> str:
    """Короткая московская запись момента относительно даты заголовка блока."""
    stamp = moscow(value)
    if stamp is None:
        return ""
    if stamp.date() == anchor.date():
        return stamp.strftime("%H:%M")
    if stamp.year == anchor.year:
        return stamp.strftime("%d.%m %H:%M")
    return stamp.strftime("%d.%m.%Y %H:%M")


def _name(event: Event) -> str:
    return event.instrument or "контракт не указан"


def _detail_lines(value: str, width: int, indent: int) -> list[str]:
    if not value:
        return []
    if width < 50:
        indent = 7
    available = max(9, width - indent)
    return [" " * indent + part for part in textwrap.wrap(
        value, width=available, break_long_words=False, break_on_hyphens=False,
        replace_whitespace=False, drop_whitespace=True,
    )]


@dataclass(frozen=True)
class Block:
    time: str
    label: str
    value: str
    details: tuple[str, ...] = ()
    role: str = ""
    data_group: bool = False
    highlight: str = ""

    def render(self, *, width: int = 96, color: bool = False) -> str:
        width = max(30, width)
        label_width = (16 if len(self.label) < 15 else len(self.label)) if self.data_group else 12
        prefix = f"{self.time or ' ' * 5}  {self.label.ljust(label_width)}  "
        lines = []
        if self.value:
            following = " " * (7 if width < 50 else 24 if self.data_group else 21)
            wrapped = textwrap.TextWrapper(width=width, initial_indent=prefix,
                subsequent_indent=following, break_long_words=False,
                break_on_hyphens=False, replace_whitespace=False).wrap(self.value)
            lines.extend(wrapped or [prefix.rstrip()])
        else:
            lines.append(prefix.rstrip())
        for detail in self.details:
            lines.extend(_detail_lines(detail, width, 24 if self.data_group else 21))
        lines = [line.replace(_GLUE, " ") for line in lines]
        if color and self.role:
            lines[0] = lines[0].replace(self.label, COLORS[self.role] + self.label + RESET, 1)
            if self.highlight:
                lines[0] = lines[0].replace(self.highlight, COLORS[self.role] + self.highlight + RESET, 1)
        return "\n".join(lines)


def _financial(event: Event) -> tuple[str, ...]:
    gross, net = event.get("gross_pnl"), event.get("net_pnl")
    if gross is None and net is None:
        return ()
    units = event.get("pnl_units") or "RUB"
    result = f"Результат: {money(gross, units=units)} до комиссий · {money(net, units=units)} после"
    source = event.get("fee_source")
    fee = money(event.get("fee"), estimate=source == "configured") if source in {"broker", "configured"} else "неизвестна"
    detail = f"Комиссия: {fee}"
    if event.get("fees_known") and event.get("fees_total") is not None:
        detail += f" · накоплено {money(event.get('fees_total'))}"
    elif event.get("fees_known") is False:
        detail += " · накопленные издержки частично неизвестны"
    return result, detail


def _stop_move(event: Event, name: str) -> tuple[str, tuple[str, ...]]:
    """Перенос стопа: «стоп перенесён A → B», причина, остаток и оценка до стопа."""
    new = price(event.get("stop"))
    old = event.get("previous_stop")
    value = f"{name} · стоп перенесён " + (f"{price(old)}{_GLUE}→{_GLUE}{new}" if old is not None else f"на {new}")
    code = str(event.get("reason") or "")
    reason = STOP_MOVE_REASONS.get(code) or (reason_label(code) if reason_label(code) != code else "")
    parts = [reason] if reason else []
    if event.get("quantity_remaining") is not None:
        parts.append(f"остаток {event.get('quantity_remaining')}")
    result = event.get("pnl_at_stop")
    if result is not None and event.get("pnl_units") == "RUB":
        amount = Decimal(str(result))
        if amount > 0:
            parts.append(f"зафиксировано {money(amount, estimate=True)} до комиссий")
        elif amount < 0:
            parts.append(f"риск до стопа {money(-amount, estimate=True)}")
        else:
            parts.append(f"риск до стопа {money(amount)}")
    detail = " · ".join(parts)
    return value, (detail[0].upper() + detail[1:],) if detail else ()


def _decision(event: Event, stamp: str) -> Block | None:
    if event.get("analysis_mode") == "historical":
        return None
    if event.get("outcome") == "no_signal" and not event.get("filtered_out"):
        return None
    if event.get("filtered_out"):
        value = _name(event)
        if event.timeframe:
            value += f" · {timeframe(event.timeframe)}"
        return Block(stamp, "ОТКЛОНЕНО ФИЛЬТРОМ", value, role="warning")
    value = _name(event)
    if event.timeframe:
        value += f" · {timeframe(event.timeframe)}"
    side = event.get("side") or ""
    value += f" · {SIDES.get(side, side or 'направление неизвестно')}"
    if event.get("price") is not None:
        value += f" · цена {price(event.get('price'), decision=True)}"
    strategy = event.get("strategy") or ""
    profile = event.get("filter_profile") or ""
    detail = strategy + (" / " + profile if strategy and profile else "")
    return Block(stamp, "СИГНАЛ", value, (detail,) if detail else (),
                 {"BUY": "buy", "SELL": "sell"}.get(side, "muted"),
                 highlight=SIDES.get(side, ""))


_CLUSTER_SHOWN = frozenset({"veto_strong", "veto", "confirm", "unknown"})


def _clusters_line(event: Event) -> tuple[str, ...]:
    """Строка «Кластеры» вердикта кластерного фильтра; для neutral и без фильтра — ничего."""
    value = event.get("clusters")
    if not isinstance(value, Mapping) or value.get("verdict") not in _CLUSTER_SHOWN or not value.get("message"):
        return ()
    return (f"Кластеры: {value['message']}",)


def _plan(event: Event, stamp: str, linked: bool) -> Block:
    count = event.get("quantity")
    count_text = f"{count} {'контракт' if count == 1 else 'контракта' if count in (2, 3, 4) else 'контрактов'}" if count is not None else "объём неизвестен"
    value = f"{count_text} · вход {price(event.get('entry'))} · стоп {price(event.get('stop'))}"
    if not linked:
        side = SIDES.get(event.get("side"), "направление неизвестно")
        value = _name(event) + " · " + side + " · " + value
    targets = event.get("targets") or ()
    details = [*_clusters_line(event), "Цели: " + (" / ".join(price(t) for t in targets) if targets else "нет") + " · ждёт подтверждения"]
    risk = event.get("risk_amount")
    risk_line = f"Риск: {money(risk)}"
    reward = event.get("reward_amount")
    expected_r = event.get("expected_r")
    if targets and reward is not None and expected_r is not None and (risk is None or risk > 0):
        risk_line += f" · план: {money(reward, estimate=True)} ({number(expected_r)}R)"
    details.append(risk_line)
    costs = event.get("costs_amount")
    cost_line = f"Издержки: {money(costs, estimate=True)}"
    constraint = event.get("limiting_constraint")
    if constraint:
        cost_line += f" · ограничение: {CONSTRAINTS.get(constraint, constraint)}"
    details.append(cost_line)
    if targets and reward is None:
        fixed = event.get("fixed_reward_amount")
        if fixed is not None and event.get("fixed_quantity"):
            details.append(f"Фиксируемая часть: {event.get('fixed_quantity')} · валовой план {money(fixed, estimate=True)}")
        details.append("Полный результат и payoff неизвестны")
    if event.get("requested_quantity") is not None:
        details.append(f"Объём: запрошено {event.get('requested_quantity')} · выбрано {count}")
    return Block("" if linked else stamp, "ПЛАН", value, tuple(details),
                 {"BUY": "buy", "SELL": "sell"}.get(event.get("side"), ""))


def _rejected(event: Event, stamp: str, linked: bool) -> Block:
    code = event.get("code") or ""
    reason = event.get("reason") or "Причина неизвестна"
    if code == "direction-not-allowed":
        reason = f"{'Продажа' if event.get('side') == 'SELL' else 'Покупка'} не разрешена для этого инструмента"
    elif code == "risk-cost-ratio":
        risk = event.get("risk_amount")
        reason = f"Риск до стопа {money(risk)} слишком мал для издержек"
    elif reason:
        reason = reason[0].upper() + reason[1:]
    details = list(_clusters_line(event))
    if code == "risk-cost-ratio" and event.get("costs_amount") is not None:
        costs = money(event.get("costs_amount"))
        slip = money(event.get("slippage_amount")) if event.get("slippage_amount") is not None else "неизвестно"
        line = f"Издержки: {costs} + {slip}"
        ratio = event.get("payoff_ratio")
        threshold = event.get("threshold")
        if ratio is not None and threshold is not None:
            ratio_text = number(ratio)
            if "," in ratio_text:
                ratio_text = ratio_text.rstrip("0").rstrip(",")
            line += f" · коэффициент {ratio_text} < порога {number(threshold)}"
        details.append(line)
    elif not linked and event.get("risk_state") == "unknown":
        details.append("Риск портфеля неизвестен: " + (event.get("unknown_reason") or "неполные данные портфеля"))
    if linked:
        value = reason
    else:
        context = _name(event)
        if event.timeframe:
            context += f" · {timeframe(event.timeframe)}"
        if event.get("side") in SIDES:
            context += f" · {SIDES[event.get('side')]}"
        if event.get("price") is not None:
            context += f" · цена {price(event.get('price'), decision=True)}"
        value = context + " · " + reason
    return Block("" if linked else stamp, "ОТКЛОНЕНО", value, tuple(details), "warning")


def _execution(event: Event, stamp: str, linked: bool = False) -> Block | None:
    kind = event.type
    name = _name(event)
    count = event.get("quantity")
    qty = str(count) if count is not None else "объём неизвестен"
    px = price(event.get("price"))
    side = SIDES.get(event.get("side"), "направление неизвестно").lower()
    details: list[str] = []
    role = ""
    time = stamp
    if kind is EventType.TARGET_HIT:
        time, label = "", "ЦЕЛЬ"
        target_number = event.get("target_number")
        value = name + (f" · №{target_number}" if target_number is not None else "") + f" · {target_price(event.get('price'))}"
        if count is not None and target_number is None:
            details.append(f"Исполнено: {qty}")
    elif kind is EventType.TRADE_CLOSED:
        label = "СДЕЛКА"
        value = f"{name} · выход {qty} по {px}"
        if event.get("quantity_remaining") is not None:
            value += f" · остаток {event.get('quantity_remaining')}"
    elif kind is EventType.ORDER_REJECTED:
        time, label, role = "" if linked else stamp, "ОРДЕР", "warning"
        value = f"{name} · {side} отклонена: {reason_label(event.get('reason'))}"
    elif kind is EventType.ORDER_ACCEPTED:
        label = "ОРДЕР"
        value = f"{name} · {side} принята · {qty} по {px}"
    elif kind in {EventType.TRADE_OPENED, EventType.POSITION_ADDED}:
        label = "ВХОД" if kind is EventType.TRADE_OPENED else "ДОБОР"
        role = {"BUY": "buy", "SELL": "sell"}.get(event.get("side"), "")
        value = f"{name} · {side} {qty} по {px}"
    elif kind is EventType.STOP_HIT:
        label, role = "СТОП", "warning"
        value = f"{name} · исполнено {qty} по {px}"
    elif kind is EventType.TRADE_CANCELLED:
        label, role = "ОТМЕНА", "warning"
        value = f"{name} · {reason_label(event.get('reason'))}"
    elif kind is EventType.STOP_MOVED:
        label = "ЗАЩИТА"
        value, moved = _stop_move(event, name)
        details.extend(moved)
    elif kind is EventType.PROTECTION_ARMED:
        label = "ЗАЩИТА"
        value = f"{name} · стоп {price(event.get('stop'))} · цель {price(event.get('take_profit'))}"
    elif kind is EventType.RISK_LIMIT_HIT:
        label, role = "ЛИМИТ РИСКА", "warning"
        value = "Новые входы и доборы запрещены"
    elif kind is EventType.CLEARING_DONE:
        label, role = "КЛИРИНГ", "muted"
        value = f"Баланс: {money(event.get('balance'))} · открыто позиций: {event.get('positions')}"
    else:
        return None
    if kind is EventType.TRADE_CLOSED and event.get("gross_pnl") is None and event.get("pnl") is not None:
        details.append(f"Результат: {money(event.get('pnl'))}")
    details.extend(_financial(event))
    return Block(time, label, value, tuple(details), role)


def _system(event: Event, stamp: str) -> Block | None:
    if event.type is EventType.HEARTBEAT:
        ticks = event.get("tick_count", 0)
        errors = event.get("error_count", 0)
        def ending(n: int, forms: tuple[str, str, str]) -> str:
            return forms[2] if 11 <= n % 100 <= 14 else forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]
        lag = event.get("feed_lag") or 0
        behind = f" · лента ядра отстаёт на {lag} {ending(lag, ('изменение', 'изменения', 'изменений'))}" if lag > 0 else ""
        return Block(stamp, "СИСТЕМА", f"{ticks} {ending(ticks, ('такт', 'такта', 'тактов'))} работы · "
                     f"{errors} {ending(errors, ('ошибка', 'ошибки', 'ошибок'))}{behind}", role="muted")
    if event.type is EventType.ERROR:
        return Block(stamp, "СБОЙ", event.get("operation") or "выполнение операции",
                     ("Подробности в bot_debug.log рядом с роботом",), "error")
    return None


def status_row(event: Event, anchor: datetime) -> str:
    state = event.get("data_state")
    value = _name(event)
    if event.timeframe:
        value += f" · {timeframe(event.timeframe)}"
    if state == "warming_up":
        value += f" · {event.get('available_bars')}/{event.get('required_bars')} свечей"
    elif state == "coverage_blocked":
        reason = event.get("reason") or "неполное покрытие"
        value += " · " + ("пробел в истории для анализа" if "пробел" in reason.lower() else reason)
    elif state == "delayed":
        expected = event.get("expected_bar")
        shown = clock(expected, anchor)
        value += " · " + (f"не получена свеча {shown}" if shown else "ожидание свечи")
    elif state == "recovered":
        value += " · данные снова доступны"
    else:
        reason = data_reason(event, lambda moment: clock(moment, anchor))
        if reason:
            value += " · " + reason
    return value


def status_label(event: Event) -> str | None:
    state = event.get("data_state")
    if state in {"current", "waiting_bar"} and not event.get("reason") and not event.get("hold"):
        return None
    if state == "rate_limited":
        return "СИСТЕМА"
    if state in DATA_LABELS:
        return DATA_LABELS[state]
    if event.get("hold"):
        return "ДАННЫЕ НЕДОСТУПНЫ"
    if event.get("reason") == UNKNOWN_SCHEDULE:
        return "РАСПИСАНИЕ НЕИЗВЕСТНО"
    if event.get("reason"):
        return "ДАННЫЕ НЕДОСТУПНЫ"
    return None


def render_status_group(events: Iterable[Event], *, width: int = 96, color: bool = False) -> str | None:
    items = tuple(events)
    if not items:
        return None
    first = items[0]
    label = status_label(first)
    anchor = headline(first, None)
    stamp = anchor.strftime("%H:%M")
    if label == "СИСТЕМА":
        return Block(stamp, label, "Лимит API · ожидание сброса", role="warning").render(width=width, color=color)
    if label is None:
        return None
    count = len(items)
    word = "пар" if 11 <= count % 100 <= 14 else "пара" if count % 10 == 1 else "пары" if 2 <= count % 10 <= 4 else "пар"
    rows = tuple(status_row(item, anchor) for item in items)
    return Block(stamp, label, f"{count} {word}", rows, "warning", data_group=True).render(width=width, color=color)


def render(event: Event, *, now: datetime | None = None,
           width: int = 96, color: bool = False, linked: bool = False) -> str | None:
    """Вернуть видимый текст одного события без завершающего перевода строки."""
    if event.type is EventType.STARTUP_ANALYSIS:
        return None
    if event.type is EventType.MARKET_DATA_STATUS:
        return render_status_group((event,), width=width, color=color)
    stamp = when(event, now)
    if event.type is EventType.DECISION:
        block = _decision(event, stamp)
    elif event.type is EventType.SIGNAL:
        block = _plan(event, stamp, linked)
    elif event.type is EventType.REJECTED:
        block = _rejected(event, stamp, linked)
    else:
        block = _system(event, stamp) or _execution(event, stamp, linked)
    return block.render(width=width, color=color) if block is not None else None
