"""Unit-тесты шаблонов: тексты уведомлений не меняются при переходе на шину."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates import render
from src.notifier.templates.decision import idle_tick_summary
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


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "пара"), (2, "пары"), (8, "пар"), (11, "пар"), (21, "пара")],
)
def test_idle_tick_summary_plural(count, expected) -> None:
    result = idle_tick_summary(datetime(2026, 10, 8, 10, 15, tzinfo=timezone.utc), count, ("GAZP",))

    assert result == f"● 10:15 ➜ ⏳ Нет сигналов ({count} {expected}: GAZP)"


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
        risk_amount=Decimal("250.00"), reward_amount=Decimal("375.00"),
        costs_amount=Decimal("14.00"), payoff_ratio=Decimal("1.44"),
    )

    assert render(event) == (
        "● NG-10.26 (1h) ➜ Сделка SELL, объём 7 — Вход: 100.5, Стоп: 101, "
        "Цели: 98, 97.25 — в работе, ждёт подтверждения, "
        "валовой план 2.50R ≈ 375 ₽ (риск 250 ₽), оценка издержек 14 ₽, чистый план 361 ₽, чистый payoff 1.44"
    )


def test_signal_without_targets_shows_none() -> None:
    event = Event.signal("NG-10.26", side="BUY", quantity=1, entry=Decimal("100"), stop=Decimal("96"))

    assert "Цели: нет" in render(event)


def test_signal_without_targets_claims_no_profit() -> None:
    """Нулевая доходность не заявляется: у плана без целей её и не существует."""
    event = Event.signal(
        "NG-10.26", side="BUY", quantity=1, entry=Decimal("3.030"), stop=Decimal("3.012"),
        targets=(), expected_r=None, timeframe="15m",
        risk_amount=Decimal("151.20"), reward_amount=Decimal("0.00"), payoff_ratio=None,
    )

    text = render(event)

    assert "Цели: нет" in text
    assert "R" not in text
    assert "0.00" not in text
    assert "151.20" not in text


def test_signal_reports_r_without_money_when_the_plan_has_no_economics() -> None:
    """План, построенный до появления денежной экономики, остаётся читаемым."""
    event = Event.signal(
        "NG-10.26", side="BUY", quantity=1, entry=Decimal("100"), stop=Decimal("96"),
        targets=(Decimal("104"),), expected_r=Decimal("1.0"),
    )

    assert render(event).endswith("валовой план 1.00R")


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
            "📝 Ордер: Заявка BUY 10 NG-10.26 по 100.0 принята",
        ),
        (
            _broker(EventType.ORDER_REJECTED, side="BUY", quantity=10,
                    price=Decimal("100.0"), reason="мало денег"),
            "📝 Ордер: Сделка BUY NG-10.26 отклонена (мало денег)",
        ),
        (
            _broker(EventType.ORDER_REJECTED, side="SELL", quantity=0, reason="trade-not-open"),
            "📝 Ордер: Сделка SELL NG-10.26 отклонена (позиция не открыта)",
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
            "❌ Отмена: Заявка NG-10.26 отменена (risk_cap)",
        ),
        (
            _broker(EventType.TRADE_CANCELLED, order_id=42, reason="entry-timeout"),
            "❌ Отмена: Заявка NG-10.26 отменена (вход не исполнен в отведённое время)",
        ),
        (
            _broker(EventType.TRADE_CANCELLED, order_id=42, reason="ttl"),
            "❌ Отмена: Заявка NG-10.26 истекла по TTL",
        ),
        (
            _broker(EventType.PROTECTION_ARMED, stop=Decimal("95.0"),
                    take_profit=Decimal("110.0")),
            "🛡 Защита: Защитный стоп 95.0 / ТП 110.0 установлен",
        ),
        (
            _broker(EventType.RISK_LIMIT_HIT),
            "⚠️ Риск: Лимит риска достигнут — требуется проверка общего бюджета",
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


def test_signal_distinguishes_gross_net_costs_sizing_and_shared_budget():
    event = Event.signal("SBER", side="BUY", quantity=2, entry=100, stop=96, targets=(106, 110),
        expected_r=2, risk_amount=80, reward_amount=160, costs_amount=40, payoff_ratio=Decimal("1.5"),
        algorithm_version="economics-v2", diagnostics={"requested_quantity": 5, "selected_quantity": 2,
            "limiting_constraint": "margin", "portfolio_pct": Decimal(2), "budget_base": Decimal(100000),
            "risk_budget": Decimal(2000), "open_risk": Decimal(1200), "pending_risk": Decimal(120),
            "free_risk": Decimal(680), "risk_excess": Decimal(0), "risk_state": "known"})
    text = render(event)
    assert "валовой план 2.00R" in text and "чистый план 120 ₽" in text and "чистый payoff 1.50" in text
    assert "запрошено 5, выбрано 2" in text and "ограничение: ГО" in text
    assert "общий бюджет 2%" in text and "свободно 680 ₽" in text
    assert "в работе, ждёт подтверждения" in text and "версия economics-v2" in text


def test_trailing_fixed_part_is_not_a_full_payoff():
    event = Event.signal("SBER", side="BUY", quantity=8, entry=100, stop=96, targets=(106, 110),
                         risk_amount=64, costs_amount=32, fixed_reward_amount=64, fixed_quantity=4)
    text = render(event)
    assert "фиксируемая часть: 4" in text and "полный результат и payoff неизвестны" in text
    assert "0.00R" not in text and "чистый payoff" not in text


def test_portfolio_excess_message_has_numbers_and_no_liquidation_claim():
    event = Event.broker_event(EventType.RISK_LIMIT_HIT, risk_scope="portfolio", portfolio_pct=2, budget_base=100000,
        risk_budget=2000, open_risk=2100, pending_risk=0, free_risk=0, risk_excess=100, risk_state="known")
    text = render(event)
    assert "превышение 100 ₽" in text and "открытый риск 2100 ₽" in text and "лимит 2000 ₽" in text
    assert "новые входы/доборы запрещены" in text
    assert "закрывается" not in text and "контр-сделк" not in text and "trade_id" not in event.payload


def test_unknown_budget_message_explains_missing_protection():
    event = Event.rejected("SBER", code="risk-state-unknown", reason="неизвестен текущий риск открытого портфеля",
        diagnostics={"risk_state": "unknown", "unknown_reason": "нет подтверждённого стопа", "portfolio_pct": Decimal(2)})
    assert "нет подтверждённого стопа" in render(event) and "свободный бюджет неизвестен" in render(event)


def test_financial_fact_keeps_known_broker_zero_and_unknown_history_distinct():
    event = Event.broker_event(EventType.TRADE_CLOSED, instrument="SBER", trade_id="internal-id", quantity=5, price=106,
        quantity_remaining=0, gross_pnl=60, net_pnl=Decimal("37.5"), fees_total=Decimal("22.5"),
        fee=0, fee_source="broker", fees_known=True, pnl_units="RUB")
    text = render(event)
    assert "комиссия исполнения 0 ₽ (брокер)" in text and "gross 60 ₽" in text and "net 37.5 ₽" in text
    assert "internal-id" not in text
    unknown = Event.broker_event(EventType.TRADE_CLOSED, instrument="SBER", quantity=5, price=106,
        quantity_remaining=0, gross_pnl=60, net_pnl=60, fees_total=0, fee=0, fee_source="unknown", fees_known=False, pnl_units="RUB")
    assert "комиссия исполнения неизвестна" in render(unknown) and "издержки частично неизвестны" in render(unknown)
