"""Постоянный консольный формат «События» из структурных полей шины."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from src.events.event import Event
from src.events.types import EventType
from src.trade_journal.export import reason_label

_COLORS = {
    "buy": "\x1b[32m",
    "sell": "\x1b[35m",
    "warning": "\x1b[33m",
    "error": "\x1b[31m",
    "muted": "\x1b[90m",
}
_RESET = "\x1b[0m"
_LABELS = {"BUY": "ПОКУПКА", "SELL": "ПРОДАЖА", "HOLD": "НЕТ СИГНАЛА"}
_CONSTRAINTS = {
    "risk": "риск", "margin": "ГО", "max-quantity": "предел количества",
    "requested-quantity": "явный запрос", "profile": "профиль",
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _number(value: Any, *, places: int = 2, signed: bool = False) -> str:
    rounded = _decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    magnitude = format(abs(rounded), f",.{places}f").replace(",", " ").replace(".", ",")
    if places == 2 and magnitude.endswith(",00"):
        magnitude = magnitude[:-3]
    sign = "−" if _decimal(value) < 0 else "+" if signed else ""
    return sign + magnitude


def _money(value: Any, *, estimate: bool = False, signed: bool = False, units: str = "RUB") -> str:
    if value is None:
        return "неизвестно"
    suffix = "₽" if units == "RUB" else units
    return f"{'≈' if estimate else ''}{_number(value, signed=signed)} {suffix}"


def _price(value: Any, *, decision: bool = False) -> str:
    if value is None:
        return "неизвестно"
    if decision:
        return format(_decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), ".3f")
    result = format(_decimal(value), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _side(event: Event) -> tuple[str, str]:
    side = event.get("side") or ""
    return _LABELS.get(side, side or "НАПРАВЛЕНИЕ НЕИЗВЕСТНО"), {"BUY": "buy", "SELL": "sell"}.get(side, "muted")


@dataclass(frozen=True)
class Block:
    when: str
    subject: str
    status: str
    role: str = ""
    suffix: str = ""
    details: tuple[str, ...] = ()
    compact: bool = False

    def render(self, *, width: int = 96, color: bool = False) -> str:
        width = max(30, width)
        subject = self.subject or "контракт не указан"
        prefix = f"{self.when}  {subject:<9}  "
        header = self.status + self.suffix
        separate = len(prefix) + len(self.status) > width
        if separate:
            subject_line = f"{self.when}  {subject}"
            prefix = " " * 7
        room = max(1, width - len(prefix))
        pieces = textwrap.wrap(header, width=room, break_long_words=True, break_on_hyphens=False)
        if not pieces:
            pieces = [""]
        lines = [subject_line] if separate else []
        status_left = len(self.status)
        for index, piece in enumerate(pieces):
            if color and self.role and status_left:
                # В узком терминале статус может перейти на вторую строку.
                cut = min(status_left, len(piece))
                piece = _COLORS[self.role] + piece[:cut] + _RESET + piece[cut:]
                status_left -= cut + 1  # textwrap убирает пробел на переносе
            lines.append((prefix if index == 0 else " " * 7) + piece)
        for detail in self.details:
            lines.extend(" " * 7 + part for part in textwrap.wrap(
                detail, width=max(12, width - 7), break_long_words=True,
                break_on_hyphens=False,
            ))
        result = "\n".join(lines)
        return result + _RESET if color else result


def _when(event: Event, tz_offset_hours: float, now: datetime | None) -> str:
    if event.bar_time is not None:
        return (event.bar_time + timedelta(hours=tz_offset_hours)).strftime("%H:%M")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")


def _context(event: Event) -> list[str]:
    lines = []
    if event.timeframe:
        lines.append(f"Период: {event.timeframe}")
    strategy = event.get("strategy")
    if strategy:
        profile = event.get("filter_profile")
        lines.append(f"Стратегия: {strategy}" + (f" · профиль {profile}" if profile else ""))
    return lines


def _budget(event: Event) -> list[str]:
    if event.get("risk_state") == "unknown":
        reason = event.get("unknown_reason") or "неполные данные портфеля"
        lines = [f"Риск портфеля неизвестен: {reason}", "Свободный бюджет: неизвестно"]
        if event.get("portfolio_pct") is not None:
            lines.insert(1, f"Общий лимит: {_number(event.get('portfolio_pct'))}%")
        return lines
    if event.get("risk_budget") is None:
        return []
    lines = [
        f"Бюджет риска: {_money(event.get('risk_budget'))}"
        + (f" · {_number(event.get('portfolio_pct'))}% от {_money(event.get('budget_base'))}"
           if event.get("portfolio_pct") is not None and event.get("budget_base") is not None else ""),
        f"Открытый риск: {_money(event.get('open_risk'))} · резервы: {_money(event.get('pending_risk'))} · свободно: {_money(event.get('free_risk'))}",
    ]
    if event.get("risk_excess") is not None and event.get("risk_excess") > 0:
        lines.append(f"Превышение: {_money(event.get('risk_excess'))}")
    return lines


def _financial(event: Event) -> list[str]:
    if event.get("gross_pnl") is None and event.get("net_pnl") is None:
        return []
    units = event.get("pnl_units") or "RUB"
    lines = [f"Результат: {_money(event.get('gross_pnl'), signed=True, units=units)} до комиссий · "
             f"{_money(event.get('net_pnl'), signed=True, units=units)} после"]
    source = event.get("fee_source")
    if source in {"configured", "broker"} and event.get("fee") is not None:
        fee = _money(event.get("fee"), estimate=source == "configured")
        note = " (оценка)" if source == "configured" else " (брокер)"
    else:
        fee, note = "неизвестна", ""
    line = f"Комиссия исполнения: {fee}{note}"
    if event.get("fees_known") and event.get("fees_total") is not None:
        line += f" · накоплено {_money(event.get('fees_total'))}"
    elif event.get("fees_known") is False:
        line += " · накопленные издержки частично неизвестны"
    lines.append(line)
    return lines


def _decision(event: Event, when: str) -> Block:
    details = _context(event)
    if event.get("filtered_out"):
        details.append("Причина: отклонено фильтром")
        return Block(when, event.instrument, "ОТКЛОНЕНО", "warning", details=tuple(details))
    if event.get("outcome") == "no_signal":
        return Block(when, event.instrument, "НЕТ СИГНАЛА", "muted", details=tuple(details))
    label, role = _side(event)
    return Block(when, event.instrument, label, role,
                 suffix=f" · {_price(event.get('price'), decision=True)}", details=tuple(details))


def _rejected(event: Event, when: str) -> Block:
    side, _ = _side(event)
    suffix = f" · {side}" if event.get("side") else ""
    if event.get("price") is not None:
        suffix += f" · {_price(event.get('price'), decision=True)}"
    details = _context(event) + [f"Причина: {event.get('reason') or 'не указана'}"]
    numbers = [(key, label, unit) for key, label, unit in (
        ("risk_amount", "риск", "RUB"), ("costs_amount", "издержки", "RUB"),
        ("slippage_amount", "проскальзывание", "RUB"),
        ("payoff_ratio", "payoff", ""), ("threshold", "порог", ""),
    ) if event.get(key) is not None]
    if numbers:
        details.append("Проверка: " + " · ".join(
            f"{label} {_money(event.get(key)) if unit else _number(event.get(key))}"
            for key, label, unit in numbers))
    details.extend(_budget(event))
    return Block(when, event.instrument, "ОТКЛОНЕНО", "warning", suffix, tuple(details))


def _signal(event: Event, when: str) -> Block:
    side, role = _side(event)
    details = _context(event)
    count = event.get("quantity")
    details.append(f"{count} {'контракт' if count == 1 else 'контракта' if count in (2, 3, 4) else 'контрактов'}"
                   f" · вход {_price(event.get('entry'))} · стоп {_price(event.get('stop'))}")
    targets = event.get("targets") or ()
    details.append("Цели: " + (" / ".join(_price(t) for t in targets) if targets else "нет")
                   + " · ждёт подтверждения")
    risk = event.get("risk_amount")
    if risk is not None:
        details.append(f"Риск: {_money(risk)}")
    else:
        details.append("Риск: неизвестно")
    expected_r = event.get("expected_r")
    reward = event.get("reward_amount")
    if targets and expected_r is not None and (risk is None or risk > 0):
        detail = "План до расходов: " + (_money(reward, estimate=True) if reward is not None else "сумма неизвестна")
        detail += f" ({_number(expected_r)}R)"
        details.append(detail)
    elif targets:
        fixed = event.get("fixed_reward_amount")
        if fixed is not None and fixed > 0 and event.get("fixed_quantity"):
            details.append(f"Фиксируемая часть: {event.get('fixed_quantity')} · валовой план {_money(fixed, estimate=True)}")
        details.append("Полный результат и payoff неизвестны")
    costs = event.get("costs_amount")
    details.append(f"Издержки: {_money(costs, estimate=True)}" if costs is not None else "Издержки: неизвестно")
    if targets and expected_r is not None and (risk is None or risk > 0):
        net = event.get("net_reward_amount")
        if net is None and reward is not None and costs is not None:
            net = reward - costs
        if net is not None:
            details.append(f"План после расходов: {_money(net, estimate=True)}")
        if event.get("payoff_ratio") is not None:
            details.append(f"Чистый payoff: {_number(event.get('payoff_ratio'))}")
    if event.get("requested_quantity") is not None:
        details.append(f"Объём: запрошено {event.get('requested_quantity')} · выбрано {count}")
    if event.get("limiting_constraint"):
        value = event.get("limiting_constraint")
        details.append(f"Ограничение: {_CONSTRAINTS.get(value, value)}")
    if event.get("algorithm_version"):
        details.append(f"Расчёт: {event.get('algorithm_version')}")
    details.extend(_budget(event))
    return Block(when, event.instrument, "ПЛАН · " + side, role, details=tuple(details))


def _execution(event: Event, when: str) -> Block | None:
    kind = event.type
    side, side_role = _side(event)
    quantity = event.get("quantity")
    price = event.get("price")
    at_price = f" по {_price(price)}" if price is not None else ""
    details: list[str] = []
    role = ""
    if kind is EventType.ORDER_ACCEPTED:
        status, suffix = "ЗАЯВКА ПРИНЯТА", f" · {side} · {quantity}{at_price}"
        role = side_role
        if event.get("order_id") is not None:
            details.append(f"Заявка: {event.get('order_id')}")
    elif kind is EventType.ORDER_REJECTED:
        status, suffix, role = "ОРДЕР ОТКЛОНЁН", f" · {side}", "warning"
        details.append(f"Причина: {reason_label(event.get('reason'))}")
    elif kind in {EventType.TRADE_OPENED, EventType.POSITION_ADDED}:
        status = "ВХОД" if kind is EventType.TRADE_OPENED else "ДОБОР"
        suffix, role = f" · {side} · {quantity}{at_price}", side_role
        if event.get("requested_quantity") is not None:
            details.append(f"Объём: запрошено {event.get('requested_quantity')} · выбрано {event.get('selected_quantity')}")
        if event.get("limiting_constraint"):
            value = event.get("limiting_constraint")
            details.append(f"Ограничение: {_CONSTRAINTS.get(value, value)}")
    elif kind is EventType.TARGET_HIT:
        status, suffix = "ЦЕЛЬ", f" · исполнено {quantity}{at_price}"
    elif kind is EventType.STOP_HIT:
        status, suffix, role = "СТОП", f" · исполнено {quantity}{at_price}", "warning"
    elif kind is EventType.TRADE_CLOSED:
        status, suffix = "ВЫХОД", f" · исполнено {quantity}{at_price}"
        if event.get("quantity_remaining") is not None:
            details.append(f"Остаток: {event.get('quantity_remaining')}")
        if event.get("gross_pnl") is None and event.get("pnl") is not None:
            details.append(f"Результат: {_money(event.get('pnl'), signed=True)}")
    elif kind is EventType.TRADE_CANCELLED:
        status, suffix, role = "ОТМЕНА", f" · заявка {event.get('order_id')}", "warning"
        details.append("Причина: истёк срок TTL" if event.get("reason") == "ttl"
                       else f"Причина: {reason_label(event.get('reason'))}")
    elif kind is EventType.PROTECTION_ARMED:
        status, suffix = "ЗАЩИТА УСТАНОВЛЕНА", ""
        details.append(f"Стоп: {_price(event.get('stop'))} · цель: {_price(event.get('take_profit'))}")
    elif kind is EventType.RISK_LIMIT_HIT:
        status, suffix, role = "ЛИМИТ РИСКА", "", "warning"
        details.extend(_budget(event))
        details.append("Новые входы и доборы запрещены" if details else "Требуется проверка общего бюджета")
    elif kind is EventType.CLEARING_DONE:
        status, suffix, role = "КЛИРИНГ", "", "muted"
        details.append(f"Баланс: {_money(event.get('balance'))} · открыто позиций: {event.get('positions')}")
    else:
        return None
    details.extend(_financial(event))
    return Block(when, event.instrument if kind is not EventType.CLEARING_DONE else "СИСТЕМА",
                 status, role, suffix, tuple(details))


def _system(event: Event, when: str) -> Block | None:
    if event.type is EventType.HEARTBEAT:
        ticks = event.get("tick_count", 0)
        tick_word = "такт" if ticks % 10 == 1 and ticks % 100 != 11 else (
            "такта" if 2 <= ticks % 10 <= 4 and not 12 <= ticks % 100 <= 14 else "тактов"
        )
        errors = event.get("error_count", 0)
        ending = "ошибка" if errors % 10 == 1 and errors % 100 != 11 else (
            "ошибки" if 2 <= errors % 10 <= 4 and not 12 <= errors % 100 <= 14 else "ошибок"
        )
        return Block(when, "СИСТЕМА", f"{ticks} {tick_word} работы · "
                     f"{errors} {ending}", "muted", compact=True)
    if event.type is EventType.ERROR:
        return Block(when, "СИСТЕМА", "СБОЙ", "error", details=(
            f"Операция: {event.get('operation') or 'выполнение операции'}",
            "Робот продолжает работу · подробности в bot_debug.log рядом с роботом",
        ))
    return None


def render(event: Event, tz_offset_hours: float = 0.0, *, now: datetime | None = None,
           width: int = 96, color: bool = False) -> str | None:
    """Цельный блок или None для типов без пользовательского представления."""
    when = _when(event, tz_offset_hours, now)
    if event.type is EventType.DECISION:
        block = _decision(event, when)
    elif event.type is EventType.REJECTED:
        block = _rejected(event, when)
    elif event.type is EventType.SIGNAL:
        block = _signal(event, when)
    else:
        block = _system(event, when) or _execution(event, when)
    return block.render(width=width, color=color) if block is not None else None
