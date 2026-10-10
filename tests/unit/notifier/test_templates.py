"""Контракт постоянного консольного формата «События»."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates import render
from src.notifier.templates.decision import idle_tick_summary

NOW = datetime(2026, 10, 9, 13, 5, tzinfo=timezone.utc)


def shown(event: Event, **options) -> str | None:
    return render(event, now=NOW, **options)


def broker(kind: EventType, **payload) -> Event:
    return Event.broker_event(kind, trade_id="internal-id", instrument="PHOR", **payload)


def test_buy_sell_filter_and_rejection_are_complete_blocks() -> None:
    buy = Event.decision("SBER", outcome="signal_buy", side="BUY", price="100.123456",
                         strategy="macd_rsi_stoch", filter_profile="raw", timeframe="15m",
                         bar_time=datetime(2026, 10, 9, 13, 15))
    sell = Event.decision("SBER", outcome="signal_sell", side="SELL", price=300)
    filtered = Event.decision("SBER", outcome="filtered", side="BUY", price=300, filtered_out=True)
    rejected = Event.rejected("SBER", reason="Размер позиции ниже минимального", side="BUY", price="100.1234")

    assert shown(buy) == (
        "13:15  SBER       ПОКУПКА · 100.123\n"
        "       Период: 15m\n"
        "       Стратегия: macd_rsi_stoch · профиль raw"
    )
    assert shown(sell) == "16:05  SBER       ПРОДАЖА · 300.000"
    assert shown(filtered) == "16:05  SBER       ОТКЛОНЕНО\n       Причина: отклонено фильтром"
    assert shown(rejected) == (
        "16:05  SBER       ОТКЛОНЕНО · ПОКУПКА · 100.123\n"
        "       Причина: Размер позиции ниже минимального"
    )


def test_bar_time_uses_configured_offset_and_publication_uses_moscow() -> None:
    event = Event.decision("SBER", outcome="signal_buy", side="BUY", price=300,
                           bar_time=datetime(2026, 10, 9, 20, 0))
    assert shown(event, tz_offset_hours=3).startswith("23:00  SBER")
    assert shown(Event.error(operation="тик")).startswith("16:05  СИСТЕМА")


@pytest.mark.parametrize("count,word", [
    (1, "проверка"), (2, "проверки"), (8, "проверок"),
    (11, "проверок"), (21, "проверка"),
])
def test_quiet_scan_counts_checks_not_pairs(count: int, word: str) -> None:
    assert idle_tick_summary(NOW, count, ("GAZP", "SBER")) == (
        f"13:05  СКАН       Сигналов нет · {count} {word}"
    )


def test_plan_is_distinct_from_confirmed_execution() -> None:
    event = Event.signal("NG-10.26", side="SELL", quantity=7, entry="100.5", stop="101",
                         targets=("98", "97.25"), expected_r="2.5", risk_amount="250",
                         reward_amount="375", costs_amount="14", payoff_ratio="1.44", timeframe="1h")
    assert shown(event) == (
        "16:05  NG-10.26   ПЛАН · ПРОДАЖА\n"
        "       Период: 1h\n"
        "       7 контрактов · вход 100.5 · стоп 101\n"
        "       Цели: 98 / 97.25 · ждёт подтверждения\n"
        "       Риск: 250 ₽\n"
        "       План до расходов: ≈375 ₽ (2,50R)\n"
        "       Издержки: ≈14 ₽\n"
        "       План после расходов: ≈361 ₽\n"
        "       Чистый payoff: 1,44"
    )
    assert "исполнено" not in shown(event)


def test_plan_with_unknown_result_does_not_claim_zero_profit() -> None:
    event = Event.signal("SBER", side="BUY", quantity=8, entry=100, stop=96,
                         targets=(106, 110), risk_amount=64, costs_amount=32,
                         fixed_reward_amount=64, fixed_quantity=4)
    text = shown(event)
    assert "Фиксируемая часть: 4 · валовой план ≈64 ₽" in text
    assert "Полный результат и payoff неизвестны" in text
    assert "0,00R" not in text


def test_plan_does_not_replace_unknown_risk_or_costs_with_zero() -> None:
    event = Event.signal("SBER", side="BUY", quantity=1, entry=100, stop=96,
                         targets=(104,), expected_r=1)
    text = shown(event)
    assert "Риск: неизвестно" in text
    assert "Издержки: неизвестно" in text
    assert "Риск: 0 ₽" not in text and "Издержки: ≈0 ₽" not in text


def test_money_is_rounded_for_display_only() -> None:
    event = Event.signal("SBER", side="BUY", quantity=1, entry=100, stop=96,
                         targets=(104,), expected_r="2.03", risk_amount="256.4433",
                         reward_amount="521.43471", costs_amount="17.43471")
    text = shown(event)
    assert "Риск: 256,44 ₽" in text
    assert "План до расходов: ≈521,43 ₽" in text
    assert "Издержки: ≈17,43 ₽" in text
    assert event.get("risk_amount") == Decimal("256.4433")
    assert event.get("costs_amount") == Decimal("17.43471")


def test_target_quantity_is_execution_volume_and_financial_facts_are_separate() -> None:
    event = broker(EventType.TARGET_HIT, quantity=4, price=5298, gross_pnl=568, net_pnl=541,
                   fee="7.5", fee_source="configured", fees_total=27, fees_known=True, pnl_units="RUB")
    assert shown(event) == (
        "16:05  PHOR       ЦЕЛЬ · исполнено 4 по 5298\n"
        "       Результат: +568 ₽ до комиссий · +541 ₽ после\n"
        "       Комиссия исполнения: ≈7,50 ₽ (оценка) · накоплено 27 ₽"
    )
    assert "№4" not in shown(event) and "internal-id" not in shown(event)


def test_loss_unknown_fee_and_raw_units() -> None:
    loss = broker(EventType.TRADE_CLOSED, quantity=5, price=106, quantity_remaining=0,
                  gross_pnl="-529.98282", net_pnl="-535.98282", fees_known=False,
                  fee_source="unknown", pnl_units="RUB")
    assert shown(loss) == (
        "16:05  PHOR       ВЫХОД · исполнено 5 по 106\n"
        "       Остаток: 0\n"
        "       Результат: −529,98 ₽ до комиссий · −535,98 ₽ после\n"
        "       Комиссия исполнения: неизвестна · накопленные издержки частично неизвестны"
    )
    raw = broker(EventType.TRADE_CLOSED, quantity=1, price=3, gross_pnl=2, net_pnl=1,
                 fee=0, fee_source="broker", fees_known=True, fees_total=0, pnl_units="RAW")
    assert "Результат: +2 RAW до комиссий · +1 RAW после" in shown(raw)
    assert "Комиссия исполнения: 0 ₽ (брокер)" in shown(raw)


def test_rejection_budget_and_long_reason_wrap_without_truncation() -> None:
    reason = "риск до стопа слишком мал относительно расчётных издержек и текущего свободного бюджета"
    event = Event.rejected("MOEX", reason=reason, side="SELL", price="152.3", diagnostics={
        "risk_amount": Decimal("3.65"), "costs_amount": Decimal("40"),
        "portfolio_pct": Decimal("2"), "budget_base": Decimal("99949.8"),
        "risk_budget": Decimal("1998.996"), "open_risk": Decimal("0"),
        "pending_risk": Decimal("260.4433"), "free_risk": Decimal("1738.537"),
        "risk_state": "known",
    })
    text = shown(event, width=52)
    assert all(len(line) <= 52 for line in text.splitlines())
    assert "Причина: риск до стопа слишком мал" in text
    assert "относительно расчётных издержек" in text
    assert "Бюджет риска: 1 999 ₽" in text
    assert "свободно: 1 738,54 ₽" in text
    assert "Проверка: риск 3,65 ₽" in text


def test_narrow_terminal_keeps_subject_and_status_visible() -> None:
    event = Event.rejected("", reason="Не хватает свободного бюджета для сделки")
    text = shown(event, width=30)
    assert all(len(line) <= 30 for line in text.splitlines())
    assert "контракт не указан" in text
    assert "ОТКЛОНЕНО" in text
    assert "Не хватает\n       свободного бюджета для" in text


def test_unknown_portfolio_budget_is_explicit() -> None:
    event = Event.rejected("SBER", reason="неизвестен риск", diagnostics={
        "risk_state": "unknown", "unknown_reason": "нет подтверждённого стопа", "portfolio_pct": 2,
    })
    text = shown(event)
    assert "Риск портфеля неизвестен: нет подтверждённого стопа" in text
    assert "Свободный бюджет: неизвестно" in text


@pytest.mark.parametrize("kind,expected", [
    (EventType.ORDER_ACCEPTED, "ЗАЯВКА ПРИНЯТА"),
    (EventType.ORDER_REJECTED, "ОРДЕР ОТКЛОНЁН"),
    (EventType.TRADE_OPENED, "ВХОД"),
    (EventType.POSITION_ADDED, "ДОБОР"),
    (EventType.STOP_HIT, "СТОП"),
    (EventType.TRADE_CLOSED, "ВЫХОД"),
    (EventType.TRADE_CANCELLED, "ОТМЕНА"),
    (EventType.PROTECTION_ARMED, "ЗАЩИТА УСТАНОВЛЕНА"),
    (EventType.RISK_LIMIT_HIT, "ЛИМИТ РИСКА"),
    (EventType.CLEARING_DONE, "КЛИРИНГ"),
])
def test_currently_visible_execution_types_still_have_a_block(kind: EventType, expected: str) -> None:
    event = Event.broker_event(kind, instrument="PHOR", side="BUY", quantity=1, price=100,
                               order_id=42, reason="trade-not-open", stop=95, take_profit=110)
    assert expected in shown(event)


def test_system_blocks_and_silent_types() -> None:
    assert shown(Event.heartbeat(tick_count=60, error_count=0)) == (
        "16:05  СИСТЕМА    60 тактов работы · 0 ошибок"
    )
    assert shown(Event.error(operation="анализ NG-10.26 (1h)")) == (
        "16:05  СИСТЕМА    СБОЙ\n"
        "       Операция: анализ NG-10.26 (1h)\n"
        "       Робот продолжает работу · подробности в bot_debug.log рядом с роботом"
    )
    for kind in (EventType.STOP_MOVED, EventType.RESERVATION_CHANGED, EventType.RATE_LIMITED):
        assert shown(Event.broker_event(kind, instrument="PHOR")) is None


@pytest.mark.parametrize("event,code", [
    (Event.decision("SBER", outcome="signal_buy", side="BUY", price=100), "32"),
    (Event.decision("SBER", outcome="signal_sell", side="SELL", price=100), "35"),
    (Event.rejected("SBER", reason="нет объёма"), "33"),
    (Event.error(operation="тик"), "31"),
    (Event.heartbeat(tick_count=1, error_count=0), "90"),
])
def test_color_roles_preserve_exact_plain_content(event: Event, code: str) -> None:
    plain = shown(event)
    colored = shown(event, color=True)
    assert f"\x1b[{code}m" in colored and colored.endswith("\x1b[0m")
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain


def test_color_does_not_leak_between_blocks() -> None:
    buy = Event.decision("SBER", outcome="signal_buy", side="BUY", price=100)
    sell = Event.decision("SBER", outcome="signal_sell", side="SELL", price=100)
    combined = shown(buy, color=True) + "\n" + shown(sell, color=True)
    assert "\x1b[0m\n" in combined
    assert "\x1b[31m" not in combined
