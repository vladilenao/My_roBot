"""Общие примитивы шаблонов: цена и заголовочная часть уведомления."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.events.event import Event


def price(value: Any) -> str:
    """Цена без хвостовых нулей."""
    if value is None:
        return ""
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def amount(value: Any) -> str:
    """Число в том виде, в каком его писал исполнитель.

    Отличие от ``price`` намеренное: цена в уведомлении о сделке печатается
    ровно так, как её посчитал брокер, поэтому ``100.0`` не превращается в
    ``100`` и запись события не меняет вид.
    """
    if value is None:
        return ""
    return format(value, "f")


def pnl_text(value: Any) -> str:
    """Финансовый результат компактной записью, как в журнале."""
    if value is None:
        return ""
    return format(float(value), "g")


def header_parts(event: Event, marker: str, tz_offset_hours: float) -> list[str]:
    """Заголовочные части: маркер с инструментом, время бара, стратегия с профилем."""
    parts: list[str] = []
    if event.instrument:
        label_block = f"{marker} {event.instrument}"
        if event.timeframe:
            label_block += f" ({event.timeframe})"
        parts.append(label_block)
    else:
        parts.append(marker)
    if event.bar_time is not None:
        parts.append(
            (event.bar_time + timedelta(hours=tz_offset_hours)).strftime("%H:%M")
        )
    strategy = event.get("strategy") or ""
    if strategy:
        strategy_block = f"| {strategy}"
        filter_profile = event.get("filter_profile") or ""
        if filter_profile:
            strategy_block += f" [{filter_profile}]"
        parts.append(strategy_block)
    return parts


def budget_text(event: Event) -> str:
    """Budget values are a projection of supplied facts, never a fresh admission."""
    if event.get("risk_state") == "unknown":
        available = f"; общий лимит {price(event.get('portfolio_pct'))}%" if event.get("portfolio_pct") is not None else ""
        return f"risk-state-unknown: общий риск неизвестен — {event.get('unknown_reason') or 'неполные данные портфеля'}{available}; свободный бюджет неизвестен"
    if event.get("risk_budget") is None:
        return ""
    text = (f"общий бюджет {price(event.get('portfolio_pct'))}% от базы {price(event.get('budget_base'))} ₽: "
            f"лимит {price(event.get('risk_budget'))} ₽, открытый риск {price(event.get('open_risk'))} ₽, "
            f"ожидающие резервы {price(event.get('pending_risk'))} ₽, свободно {price(event.get('free_risk'))} ₽")
    if event.get("risk_excess") is not None and event.get("risk_excess") > Decimal(0):
        text += f", превышение {price(event.get('risk_excess'))} ₽"
    return text


def financial_text(event: Event) -> str:
    if event.get("gross_pnl") is None:
        return ""
    units = "₽" if event.get("pnl_units") == "RUB" else "RAW"
    source = event.get("fee_source")
    fee = (f"комиссия исполнения {price(event.get('fee'))} ₽ ({'оценка' if source == 'configured' else 'брокер'})"
           if source in {"configured", "broker"} else "комиссия исполнения неизвестна")
    text = (f"{fee}; по сделке gross {price(event.get('gross_pnl'))} {units}, "
            f"учётный net {price(event.get('net_pnl'))} {units}")
    if event.get("fees_known"):
        text += f", накопленные комиссии {price(event.get('fees_total'))} ₽"
    else:
        text += " (издержки частично неизвестны)"
    return text
