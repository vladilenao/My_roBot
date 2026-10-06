"""Шаблон рекомендации: сделка допущена, заявка ждёт подтверждения."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates.common import budget_text, price

RUB_SUFFIX = "₽"


def render(event: Event, tz_offset_hours: float = 0.0) -> str | None:
    if event.type is not EventType.SIGNAL:
        return None
    label = f"● {event.instrument}"
    if event.timeframe:
        label += f" ({event.timeframe})"
    targets = ", ".join(price(target) for target in event.get("targets", ())) or "нет"
    text = (
        f"{label} ➜ Сделка {event.get('side')}, объём {event.get('quantity', 0)} — "
        f"Вход: {price(event.get('entry'))}, "
        f"Стоп: {price(event.get('stop'))}, Цели: {targets} "
        f"— в работе, ждёт подтверждения"
    )
    block = _result_block(event)
    if block:
        text += f", {block}"
    if event.get("expected_r") is None and event.get("risk_amount") is not None:
        text += f", риск до стопа {_money(event.get('risk_amount'), '')}"
        if event.get("costs_amount") is not None:
            text += f", оценка круговых издержек {_money(event.get('costs_amount'), '')}"
    requested = event.get("requested_quantity")
    if requested is not None:
        text += f"; запрошено {requested}, выбрано {event.get('quantity')}"
    constraint = event.get("limiting_constraint")
    if constraint:
        labels = {"risk": "риск", "margin": "ГО", "max-quantity": "предел количества", "requested-quantity": "явный запрос", "profile": "профиль"}
        text += f"; ограничение: {labels.get(constraint, constraint)}"
    if event.get("algorithm_version"):
        text += f"; версия {event.get('algorithm_version')}"
    budget = budget_text(event)
    if budget:
        text += f"; {budget}"
    return text


def _result_block(event: Event) -> str:
    """Ожидаемый результат вместе с деньгами — или ничего вовсе.

    Блок печатается только когда результат известен и риск положителен: у плана
    без целей нулевая доходность не утверждается, потому что `0.00R` трейдер
    прочитал бы как «эта сделка ничего не принесёт», а не как «результат не
    определён».
    """
    expected_r = event.get("expected_r")
    risk_amount = event.get("risk_amount")
    reward_amount = event.get("reward_amount")
    if expected_r is None or risk_amount is not None and risk_amount <= 0:
        fixed = event.get("fixed_reward_amount")
        quantity = event.get("fixed_quantity", 0)
        if fixed is not None and fixed > 0 and quantity:
            return f"фиксируемая часть: {quantity} контрактов, валовая выручка {_money(fixed, '')}; полный результат и payoff неизвестны"
        return "полный результат и payoff неизвестны" if event.get("targets") else ""
    risks = f" ({_money(risk_amount, 'риск ')})" if risk_amount else ""
    money = f" ≈ {_money(reward_amount, '')}" if reward_amount else ""
    result = f"валовой план {_ratio(expected_r)}R{money}{risks}"
    costs = event.get("costs_amount")
    if costs is not None:
        result += f", оценка издержек {_money(costs, '')}"
        net = event.get("net_reward_amount")
        if net is None and reward_amount is not None:
            net = reward_amount-costs
        if net is not None:
            result += f", чистый план {_money(net, '')}"
    if event.get("payoff_ratio") is not None:
        result += f", чистый payoff {_ratio(event.get('payoff_ratio'))}"
    return result


def _ratio(value: Any) -> str:
    """Доли риска всегда с двумя знаками: `1.50R` читается как одно и то же число."""
    return f"{Decimal(str(value)):.2f}"


def _money(value: Any, label: str) -> str:
    """Деньги печатаются без хвостовых нулей: 375.00 ₽ читается как 375 ₽."""
    return f"{label}{price(value)} {RUB_SUFFIX}"
