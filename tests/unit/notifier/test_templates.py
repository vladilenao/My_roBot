"""Unit-тесты шаблонов: тексты уведомлений не меняются при переходе на шину."""

from datetime import datetime
from decimal import Decimal

import pytest

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates import render
from src.strategies.contracts import Decision, SignalType


def _decision(signal_type=SignalType.BUY, price=Decimal("1234.5678")) -> Decision:
    return Decision(signal_type, price, strategy_name="macd_rsi_stoch")


def test_buy_decision_text() -> None:
    event = Event.decision(
        "NG-10.26", outcome="signal_buy", side="BUY", price=Decimal("1234.5678"),
        strategy="macd_rsi_stoch", filter_profile="basic_levels", timeframe="15m",
        bar_time=datetime(2026, 9, 26, 22, 45),
    )

    assert render(event) == "● NG-10.26 (15m) 22:45 | macd_rsi_stoch [basic_levels] ➜ 🟢 ПОКУПКА (BUY) — Цена: 1234.568"


def test_sell_decision_text() -> None:
    # round(Decimal, 3) печатает три знака после запятой — прежнее поведение сохранено
    event = Event.decision("SBER", outcome="signal_sell", side="SELL", price=Decimal("300"))

    assert render(event) == "● SBER ➜ 🔴 ПРОДАЖА (SELL) — Цена: 300.000"


def test_hold_decision_text() -> None:
    event = Event.decision("SBER", outcome="no_signal", side="HOLD", price=None, strategy="")

    assert render(event) == "● SBER ➜ ⏳ Нет сигнала."


def test_filtered_decision_text() -> None:
    event = Event.decision("SBER", outcome="filtered", side="BUY", price=Decimal("300"), filtered_out=True)

    assert render(event) == "● SBER ➜ ❌ Отклонено фильтром."


def test_rejection_text_keeps_marker_and_reason() -> None:
    event = Event.rejected("SBER", reason="Размер позиции ниже минимального", side="BUY")

    assert render(event) == "⛔ SBER ➜ Сделка не допущена: Размер позиции ниже минимального"


def test_timezone_offset_applies_to_bar_time() -> None:
    event = Event.decision(
        "SBER", outcome="signal_buy", side="BUY", price=Decimal("300"),
        bar_time=datetime(2026, 9, 26, 20, 0),
    )

    assert render(event, tz_offset_hours=3) == "● SBER 23:00 ➜ 🟢 ПОКУПКА (BUY) — Цена: 300.000"


def test_signal_text_reports_plan_quantity_targets_and_expected_r() -> None:
    event = Event.signal(
        "NG-10.26", side="SELL", quantity=7, entry=Decimal("100.5"), stop=Decimal("101"),
        targets=(Decimal("98"), Decimal("97.25")), expected_r=Decimal("2.5"), timeframe="1h",
    )

    assert render(event) == (
        "● NG-10.26 (1h) ➜ Сделка SELL, объём 7 — Вход: 100.5, Стоп: 101, "
        "Цели: 98, 97.25 — в работе, ждёт подтверждения, ожидаемый результат 2.5R"
    )


def test_signal_without_targets_shows_none() -> None:
    event = Event.signal("NG-10.26", side="BUY", quantity=1, entry=Decimal("100"), stop=Decimal("96"))

    assert "Цели: нет" in render(event)


def _broker(event_type, **payload) -> Event:
    return Event.broker_event(
        event_type, trade_id="trade-1", instrument="NG-10.26", **payload
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            _broker(EventType.ORDER_ACCEPTED, side="BUY", quantity=10,
                    price=Decimal("100.0"), order_id=42),
            "📝 Ордер: Заявка BUY 10 NG-10.26 по 100.0 принята (id=42)",
        ),
        (
            _broker(EventType.ORDER_REJECTED, side="BUY", quantity=10,
                    price=Decimal("100.0"), reason="мало денег"),
            "📝 Ордер: Сделка BUY NG-10.26 отклонена (мало денег)",
        ),
        (
            _broker(EventType.TRADE_OPENED, side="BUY", quantity=10, price=Decimal("100.0")),
            "💰 Сделка: Вход BUY 10 NG-10.26 по 100.0",
        ),
        (
            _broker(EventType.POSITION_ADDED, side="SELL", quantity=3, price=Decimal("105.5")),
            "➕ Добор: Добор SELL 3 NG-10.26 по 105.5",
        ),
        (
            _broker(EventType.TARGET_HIT, quantity=2, price=Decimal("110.0")),
            "🎯 Цель: Цель 2 NG-10.26 по 110.0",
        ),
        (
            _broker(EventType.STOP_HIT, quantity=2, price=Decimal("95.0")),
            "🛑 Стоп: Защитный стоп 2 NG-10.26 по 95.0",
        ),
        (
            _broker(EventType.TRADE_CLOSED, quantity=2, price=Decimal("110.0"),
                    pnl=Decimal("200.0")),
            "💰 Сделка: Закрытие 2 NG-10.26 по 110.0 (PnL 200)",
        ),
        (
            _broker(EventType.TRADE_CLOSED, quantity=2, price=Decimal("110.0"),
                    pnl=Decimal("-20.5"), reason="protective"),
            "💰 Сделка: Закрытие позиции NG-10.26 2 шт: PnL -20.5 руб",
        ),
        (
            _broker(EventType.TRADE_CANCELLED, order_id=42, reason="risk_cap"),
            "❌ Отмена: Заявка 42 отменена (risk_cap)",
        ),
        (
            _broker(EventType.TRADE_CANCELLED, order_id=42, reason="ttl"),
            "❌ Отмена: Заявка 42 истекла по TTL",
        ),
        (
            _broker(EventType.PROTECTION_ARMED, stop=Decimal("95.0"),
                    take_profit=Decimal("110.0")),
            "🛡 Защита: Защитный стоп 95.0 / ТП 110.0 установлен",
        ),
        (
            _broker(EventType.RISK_LIMIT_HIT),
            "⚠️ Over-risk: Лимит перекоса достигнут — позиция закрывается контр-сделкой",
        ),
    ],
)
def test_executor_event_text_keeps_wording(event: Event, expected: str) -> None:
    assert render(event) == expected


def test_clearing_text_reports_balance_and_positions_without_contract() -> None:
    event = Event.clearing_done(balance=Decimal("100000"), positions=2)

    assert render(event) == "🏛 Клиринг: снимок баланса 100000 руб, открыто позиций: 2"


def test_trade_id_is_correlation_only_and_never_shown() -> None:
    event = _broker(EventType.TRADE_CLOSED, quantity=2, price=Decimal("110.0"), pnl=1)

    assert event.get("trade_id") == "trade-1"
    assert "trade-1" not in render(event)


def test_event_without_published_wording_renders_nothing() -> None:
    assert render(_broker(EventType.RESERVATION_CHANGED, quantity=1)) is None


def test_heartbeat_and_error_text() -> None:
    assert render(Event.heartbeat(tick_count=10, error_count=2)) == (
        "💓 Сердцебиение: тиков работы — 10, ошибок за период — 2."
    )
    assert render(Event.error(operation="анализ NG-10.26 (1h)")) == (
        "❗ Сбой: анализ NG-10.26 (1h). Робот продолжает работу. "
        "Подробности — в bot_debug.log рядом с роботом."
    )


def test_rate_limited_event_has_no_text() -> None:
    assert render(Event.rate_limited(source="tinkoff")) is None


def test_decision_price_is_rounded_to_three() -> None:
    event = Event.decision("SBER", outcome="signal_buy", side="BUY", price=Decimal("100.123456"))

    assert render(event).endswith("Цена: 100.123")


def test_decision_payload_accepts_real_decision_object() -> None:
    decision = _decision()

    event = Event.decision(
        "SBER", outcome="signal_buy", side=decision.signal_type.name,
        price=decision.price, strategy=decision.strategy_name or "",
    )

    assert event.get("side") == "BUY"
