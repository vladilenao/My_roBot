"""``probe``: лимитная заявка далеко от рынка — ключ, поиск, повтор, стримы, снятие, уборка."""

from __future__ import annotations

from decimal import Decimal

from src.finam.orders import is_active


def placed(broker) -> int:
    return sum(1 for method, _ in broker.calls if method == "orders.PlaceOrder")


def active(broker) -> list:
    return [state for state in broker.orders.values() if state.order_id and is_active(state)]


def test_full_limit_probe(run, broker):
    result = run("probe", "NG")
    assert result.code == 0, result.report()
    for code in ("Ф0.10", "Ф0.11", "Ф0.12", "Ф0.13", "Ф0.14", "Ф0.15", "Ф0.16", "Ф0.17", "Ф0.99"):
        assert result.row(code)[0] == "да", (code, result.row(code))
    assert "не по шагу отклонена" in result.row("Ф0.11")[1]
    assert "тот же ключ после ошибки принят" in result.row("Ф0.12")[1]
    assert "повтор отклонён: ALREADY_EXISTS" in result.row("Ф0.14")[1]
    events = result.row("Ф0.15")[1]
    assert "SubscribeOrders: 1 событий, номер в первом совпал" in events
    assert "SubscribeOrderTrade: 1 событий, номер в первом совпал" in events
    assert "CANCELED" in result.row("Ф0.17")[1]
    # Покупка ниже рынка на 3%, к шагу 0.001: 3.000 * 0.97 = 2.910.
    assert "лимит 2.91" in result.row("Ф0.12")[1]
    assert active(broker) == []
    # Номер счёта в описании ошибки брокера показан последними 4 символами.
    assert "FAKE-FINAM" not in result.row("Ф0.14")[1]


def test_key_repeat_creates_second_order(run, broker):
    broker.repeat_key = "new"
    result = run("probe", "NG")
    outcome, detail = result.row("Ф0.14")
    assert outcome == "нет"
    assert "повтор создал вторую заявку" in detail and "вторая снята сразу" in detail
    assert active(broker) == []


def test_key_after_error_refused(run, broker):
    broker.repeat_after_error = "reject"
    result = run("probe", "NG")
    outcome, detail = result.row("Ф0.12")
    assert outcome == "нет"
    assert "повтор ключа после ошибки отклонён" in detail
    assert active(broker) == []


def test_price_band_retries_with_half_offset(run, broker):
    broker.band_pct = Decimal(2)
    result = run("probe", "NG")
    outcome, detail = result.row("Ф0.12")
    assert outcome == "не проверено"
    assert "цена вне лимитов" in detail
    assert "лимит 2.955" in detail  # 3.000 * (1 - 1.5%) = 2.955
    assert "отклонена" in result.report()
    assert active(broker) == []


def test_no_trading_no_orders(run, broker):
    broker.trading = False
    result = run("probe", "NG")
    assert result.code == 1
    assert result.row("Ф0.10")[0] == "нет"
    assert result.row("Ф0.12") == ("не проверено", "торги не идут")
    assert placed(broker) == 0


def test_failure_after_place_still_cleans_up(run, broker):
    broker.fail_after_place = True
    result = run("probe", "NG")
    outcome, detail = result.row("Ф0.13")
    assert outcome == "ошибка" and "UNAVAILABLE" in detail
    assert result.row("Ф0.99")[0] == "да"
    assert active(broker) == []
    assert (result.run_dirs()[-1] / "responses.jsonl").exists()


def test_unexpected_fill_stops_without_closing(run, broker):
    broker.reject_off_step = False
    broker.fill_limit = True
    result = run("probe", "NG")
    assert result.code == 1
    assert "заявка разведки исполнилась" in result.report()
    # Позиция осталась: разведка её не закрывает, рыночных заявок не было.
    assert broker.positions
    assert all(state.order.type != 1 for state in broker.orders.values())  # ORDER_TYPE_MARKET


def test_rate_limit_on_place_is_not_retried(run, broker):
    broker.exhaust_place = True
    result = run("probe", "NG")
    assert placed(broker) == 2  # ошибочная заявка и первая правильная — без повторов
    outcome, detail = result.row("Ф0.12")
    assert outcome == "ошибка" and "RESOURCE_EXHAUSTED" in detail


def test_share_quantity_is_one_lot_in_shares(run, broker):
    result = run("probe", "SBER")
    assert result.code == 0, result.report()
    assert "SBER LIMIT покупка 10" in result.row("Ф0.12")[1]


def test_clearing_is_not_trading(run, broker):
    broker.session_type = "CLEARING"
    result = run("probe", "NG")
    assert result.code == 1
    assert "сессия сейчас: CLEARING" in result.row("Ф0.10")[1]
    assert placed(broker) == 0


def test_no_money_stops_without_retries(run, broker):
    broker.no_money = True
    result = run("probe", "NG")
    outcome, detail = result.row("Ф0.12")
    assert outcome == "ошибка"
    assert detail.startswith("недостаточно обеспечения — пополните счёт: INVALID_ARGUMENT [145]No enough coverage.")
    assert "\x0c" not in result.report() and " / Недостаток обеспечения" in detail
    assert "INVALID_ARGUMENT INVALID_ARGUMENT" not in detail
    assert placed(broker) == 2  # ошибочная по шагу и одна правильная — без повторов
