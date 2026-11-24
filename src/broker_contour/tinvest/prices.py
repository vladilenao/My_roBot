"""Суммы Т-Инвест в пункты (add-tinvest-orders, design.md решение 9; перенесено refactor-broker-port).

У фьючерса Т-Инвест отдаёт суммы заявки и цены ``OrderStage`` в рублях за контракт: стоимость шага
в рублях переводит их в пункты.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from src.broker_contour.prices import PriceCard, to_step


def points_from_rub(card: PriceCard, rub: Decimal) -> Decimal:
    """Рубли за контракт фьючерса → пункты: ``рубли × шаг / стоимость шага``, на сетке шага.

    22 731,55 ₽ при шаге 0.001 и стоимости шага 8,08664 ₽ → 2.811.
    """
    if not card.future or card.step_rub <= 0:
        return rub
    points = rub * card.price_step / card.step_rub
    return ((points / card.price_step).to_integral_value(ROUND_HALF_UP) * card.price_step).quantize(card.price_step)


def trade_price(card: PriceCard, price: Decimal, in_rub: bool) -> Decimal:
    return points_from_rub(card, price) if in_rub else price


def fill_average(card: PriceCard, executed_rub: Decimal, lots_executed: int, initial_rub: Decimal | None = None,
                 lots_requested: int = 0) -> Decimal:
    """Средняя цена исполнения из суммы ``executed_order_price``, когда сделок нет ни в стриме, ни в ``stages``.

    Разведка П10–П16 (1 лот) не различает, сумма это за все исполненные лоты или цена одного лота.
    Ориентир — цена лота заявки ``initial_order_price / lots_requested``: из двух прочтений берётся
    ближайшее к нему. Итог — цена инструмента: пункты у фьючерса, цена бумаги у акции.
    """
    per_lot = executed_rub / lots_executed if lots_executed > 1 else executed_rub
    if lots_executed > 1 and initial_rub and lots_requested > 0:
        reference = initial_rub / lots_requested
        if abs(executed_rub - reference) < abs(per_lot - reference):
            per_lot = executed_rub
    if card.future:
        return points_from_rub(card, per_lot)
    return to_step(per_lot / card.lot, card.price_step, ROUND_HALF_UP)
