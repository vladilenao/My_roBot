"""Смысловые проверки консольных шаблонов поверх буквальных эталонов."""

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


def test_market_status_is_event_first_and_omits_transport_diagnostics() -> None:
    diagnostics = dict(session="open", observed_at=datetime(2026, 10, 13, 11, 30, 12),
        schedule={"source": "tbank", "digest": "bb4515efb582"},
        ingestion={"transport": "stream", "epoch": 1, "data_hold": "READY"})
    assert shown(Event.market_data_status("PHOR", timeframe="1h", data_state="current", **diagnostics)) is None
    limited = Event.market_data_status("SBER", timeframe="15m", data_state="rate_limited",
        reason="Ожидание сброса общего лимита API", **diagnostics)
    assert shown(limited) == "14:30  СИСТЕМА       Лимит API · ожидание сброса"
    assert "bb4515efb582" not in shown(limited)
    delayed = Event.market_data_status("VTBR", timeframe="15m", session="open",
        data_state="delayed", observed_at=datetime(2026, 10, 13, 13, 15),
        expected_bar=datetime(2026, 10, 13, 13, 0))
    assert shown(delayed) == (
        "16:15  ЗАДЕРЖКА ДАННЫХ  1 пара\n"
        "                        VTBR · 15м · не получена свеча 16:00"
    )


def test_startup_overview_is_silent_even_with_signal_or_reason() -> None:
    overview = Event.startup_analysis("SBER", timeframe="15m", published_at=NOW,
        results=[{"outcome": "BUY", "reason": "неполная история"}],
        available_bars=42, required_bars=60, coverage_complete=False, reason="неполная история")
    assert shown(overview) is None


def test_decision_and_standalone_rejection_keep_event_first_context() -> None:
    buy = Event.decision("SBER", outcome="signal_buy", side="BUY", price="100.123456",
        strategy="macd_rsi_stoch", filter_profile="raw", timeframe="15m", bar_time=NOW)
    assert shown(buy) == (
        "16:05  СИГНАЛ        SBER · 15м · ПОКУПКА · цена 100.123\n"
        "                     macd_rsi_stoch / raw"
    )
    filtered = Event.decision("SBER", outcome="filtered", side="BUY", filtered_out=True)
    assert shown(filtered).startswith("16:05  ОТКЛОНЕНО ФИЛЬТРОМ  SBER")
    rejected = Event.rejected("SBER", reason="Размер позиции ниже минимального", side="BUY")
    assert shown(rejected) == "16:05  ОТКЛОНЕНО     SBER · ПОКУПКА · Размер позиции ниже минимального"


def test_bar_time_offset_and_moscow_publication_are_separate() -> None:
    decision = Event.decision("SBER", outcome="signal_buy", side="BUY", price=300,
        bar_time=datetime(2026, 10, 9, 20, 0))
    assert shown(decision).startswith("23:00  СИГНАЛ")
    assert shown(Event.error(operation="тик")).startswith("16:05  СБОЙ")


@pytest.mark.parametrize("count,word", [
    (1, "пара"), (2, "пары"), (8, "пар"), (11, "пар"), (21, "пара"),
])
def test_quiet_scan_counts_pairs(count: int, word: str) -> None:
    assert idle_tick_summary(NOW, count) == (
        f"13:05  СКАНИРОВАНИЕ  Сигналов нет · проверено {count} {word}"
    )


def test_plan_is_distinct_from_confirmed_execution() -> None:
    event = Event.signal("NG-10.26", side="SELL", quantity=7, entry="100.5", stop="101",
        targets=("98", "97.25"), expected_r="2.5", risk_amount="250",
        reward_amount="375", costs_amount="14", timeframe="1h")
    text = shown(event)
    assert text.startswith("16:05  ПЛАН          NG-10.26 · ПРОДАЖА · 7 контрактов · вход 100.5 · стоп 101")
    assert "Цели: 98 / 97.25 · ждёт подтверждения" in text
    assert "Риск: 250 ₽ · план: ~375 ₽ (2,50R)" in text
    assert "Издержки: ~14 ₽" in text
    assert "исполнено" not in text


def test_unknown_plan_does_not_invent_profit_or_risk() -> None:
    event = Event.signal("SBER", side="BUY", quantity=8, entry=100, stop=96,
        targets=(106, 110), fixed_reward_amount=64, fixed_quantity=4)
    text = shown(event)
    assert "Риск: неизвестно" in text
    assert "Издержки: неизвестно" in text
    assert "Фиксируемая часть: 4 · валовой план ~64 ₽" in text
    assert "Полный результат и payoff неизвестны" in text


def test_money_rounding_changes_only_presentation() -> None:
    event = Event.signal("SBER", side="BUY", quantity=1, entry=100, stop=96,
        targets=(104,), expected_r="2.03", risk_amount="256.4433",
        reward_amount="521.43471", costs_amount="17.43471")
    text = shown(event)
    assert "Риск: 256,44 ₽ · план: ~521,43 ₽ (2,03R)" in text
    assert "Издержки: ~17,43 ₽" in text
    assert event.get("risk_amount") == Decimal("256.4433")


def test_target_number_is_not_execution_quantity() -> None:
    event = broker(EventType.TARGET_HIT, quantity=4, price=Decimal("5298.0"),
        gross_pnl=568, net_pnl=541, fee="7.5", fee_source="configured",
        fees_total=27, fees_known=True)
    text = shown(event)
    assert text.startswith("       ЦЕЛЬ          PHOR · 5 298,0 ₽")
    assert "№4" not in text
    assert "Исполнено: 4" in text
    numbered = broker(EventType.TARGET_HIT, quantity=2, target_number=4, price=Decimal("5298.0"))
    assert "PHOR · №4 · 5 298,0 ₽" in shown(numbered)
    assert "internal-id" not in shown(numbered)


def test_loss_unknown_commission_and_raw_units() -> None:
    loss = broker(EventType.TRADE_CLOSED, quantity=5, price=106, quantity_remaining=0,
        gross_pnl="-529.98282", net_pnl="-535.98282", fees_known=False, pnl_units="RUB")
    text = shown(loss)
    assert "Результат: −529,98 ₽ до комиссий · −535,98 ₽ после" in text
    assert "Комиссия: неизвестна · накопленные издержки частично неизвестны" in text
    raw = broker(EventType.TRADE_CLOSED, quantity=1, price=3, gross_pnl=2, net_pnl=1,
        fee=0, fee_source="broker", fees_known=True, fees_total=0, pnl_units="RAW")
    assert "Результат: 2 RAW до комиссий · 1 RAW после" in shown(raw)


@pytest.mark.parametrize("kind,label", [
    (EventType.ORDER_ACCEPTED, "ОРДЕР"), (EventType.ORDER_REJECTED, "ОРДЕР"),
    (EventType.TRADE_OPENED, "ВХОД"), (EventType.POSITION_ADDED, "ДОБОР"),
    (EventType.STOP_HIT, "СТОП"), (EventType.TRADE_CLOSED, "СДЕЛКА"),
    (EventType.TRADE_CANCELLED, "ОТМЕНА"), (EventType.PROTECTION_ARMED, "ЗАЩИТА"),
    (EventType.RISK_LIMIT_HIT, "ЛИМИТ РИСКА"), (EventType.CLEARING_DONE, "КЛИРИНГ"),
])
def test_other_visible_types_remain_event_first(kind: EventType, label: str) -> None:
    event = broker(kind, side="BUY", quantity=1, price=100, stop=95, take_profit=110)
    text = shown(event)
    assert text is not None and label in text.splitlines()[0]
    assert text.splitlines()[0].find(label) < text.splitlines()[0].find("PHOR") if "PHOR" in text.splitlines()[0] else True


def test_silent_types_and_color_roles() -> None:
    for kind in (EventType.STOP_MOVED, EventType.RESERVATION_CHANGED, EventType.RATE_LIMITED):
        assert shown(broker(kind)) is None
    for event, code in (
        (Event.decision("SBER", outcome="signal_buy", side="BUY", price=100), "32"),
        (Event.decision("SBER", outcome="signal_sell", side="SELL", price=100), "35"),
        (Event.rejected("SBER", reason="нет объёма"), "33"),
        (Event.error(operation="тик"), "31"),
        (Event.heartbeat(tick_count=1, error_count=0), "90"),
    ):
        colored = shown(event, color=True)
        assert f"\x1b[{code}m" in colored
        assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == shown(event)


def test_narrow_terminal_wraps_without_losing_reason() -> None:
    event = Event.rejected("MOEX", reason="Не хватает свободного бюджета для сделки")
    text = shown(event, width=30)
    assert all(len(line) <= 30 for line in text.splitlines())
    assert "Не хватает свободного бюджета для сделки" in " ".join(line.strip() for line in text.splitlines()).replace("  ", " ")


def test_execution_barrier_hold_is_moscow_time_without_zone() -> None:
    held = Event.market_data_status("SBER", timeframe="15m", session="open", data_state="waiting_bar",
        observed_at=datetime(2026, 10, 15, 8, 0, 5, tzinfo=timezone.utc),
        hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)})
    assert shown(held) == (
        "11:00  ДАННЫЕ НЕДОСТУПНЫ  1 пара\n"
        "                        SBER · 15м · Удержано: базовая 1m не подтверждена до 11:00"
    )


def _status(instrument: str, observed_at: datetime, **payload) -> Event:
    return Event.market_data_status(instrument, timeframe="15m", session="open",
        data_state=payload.pop("data_state", "waiting_bar"), observed_at=observed_at, **payload)


def test_hold_moments_are_short_relative_to_group_date() -> None:
    utc = timezone.utc
    morning = datetime(2026, 10, 15, 4, 0, tzinfo=utc)
    night_gap = _status("SBER", morning, hold={"kind": "gap", "state": "active",
        "lower": datetime(2026, 10, 14, 20, 45, tzinfo=utc), "upper": datetime(2026, 10, 14, 21, 0, tzinfo=utc)})
    assert shown(night_gap).endswith("SBER · 15м · Восстановление пропуска 14.10 23:45–00:00")
    new_year = _status("SBER", datetime(2027, 1, 2, 7, 0, tzinfo=utc), hold={"kind": "integrity",
        "minute": datetime(2026, 12, 31, 15, 0, tzinfo=utc)})
    assert shown(new_year).endswith("Удержано: изменена уже исполненная минута 31.12.2026 18:00")
    parked = _status("SBER", datetime(2026, 10, 15, 7, 20, tzinfo=utc), hold={"kind": "gap", "state": "parked",
        "lower": datetime(2026, 10, 15, 7, 0, tzinfo=utc), "upper": datetime(2026, 10, 15, 7, 15, tzinfo=utc),
        "next_retry_at": datetime(2026, 10, 15, 7, 30, tzinfo=utc)})
    assert " ".join(shown(parked).split()).endswith("Пропуск 10:00–10:15 не восстановлен; следующая проверка 10:30")
    unknown = _status("SBER", morning, hold={"kind": "integrity", "minute": None})
    assert shown(unknown).endswith("Удержано: изменена уже исполненная минута")
    for event in (night_gap, new_year, parked, unknown):
        assert "UTC" not in shown(event) and "МСК" not in shown(event)


def test_hold_with_unknown_schedule_stays_a_data_hold() -> None:
    held = _status("GAZP", datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
        reason="Применимое торговое расписание неизвестно",
        hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)})
    assert shown(held) == (
        "11:00  ДАННЫЕ НЕДОСТУПНЫ  1 пара\n"
        "                        GAZP · 15м · Удержано: базовая 1m не подтверждена до 11:00; расписание\n"
        "                        неизвестно"
    )


def test_console_time_ignores_computer_timezone(monkeypatch) -> None:
    import time
    decision = Event.decision("SBER", outcome="signal_buy", side="BUY", price=300,
        bar_time=datetime(2026, 10, 15, 7, 0))
    status = _status("SBER", datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
        hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8, 0)})
    outputs = []
    try:
        for zone in ("UTC", "Europe/Moscow", "America/New_York"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            outputs.append((shown(decision), shown(status)))
    finally:
        monkeypatch.undo()
        time.tzset()
    assert len(set(outputs)) == 1
    assert outputs[0][0].startswith("10:00  СИГНАЛ")


def test_telegram_keeps_its_own_dated_hold_phrase() -> None:
    from src.notifier.telegram_templates import render as telegram_render
    held = _status("SBER", datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
        hold={"kind": "execution_barrier", "through": datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)})
    assert "Причина: Удержано: базовая 1m не подтверждена до 15.10.2026 11:00:00 МСК" in telegram_render(
        held, tz_offset_hours=3)
