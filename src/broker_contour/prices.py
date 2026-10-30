"""Цены, лоты и ГО заявок контура на брокере (add-tinvest-orders, design.md решения 9 и 10).

Карточка цены строится из ``ContractMeta`` ядра: шаг цены и стоимость шага.
У фьючерса стоимость шага в рублях переводит суммы брокера в пункты, у акции
``step_cost = шаг × лот`` даёт размер лота. ГО фьючерса уточняется
``GetFuturesMargin`` песочницы и заменяет ГО карточки ядра.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class PriceCard:
    uid: str
    price_step: Decimal
    step_rub: Decimal
    lot: int = 1
    future: bool = True
    go_buy: Decimal | None = None
    go_sell: Decimal | None = None

    def go(self, side: str) -> Decimal | None:
        return self.go_buy if side == "BUY" else self.go_sell


def card_from_meta(uid: str, meta) -> PriceCard:
    """Карточка из ``ContractMeta``: фьючерс — с датой экспирации, у акции лот = стоимость шага / шаг."""
    step = Decimal(str(meta.price_step))
    step_rub = Decimal(str(meta.step_cost))
    future = getattr(meta, "expiration_date", None) is not None
    lot = 1 if future or step <= 0 else max(1, int((step_rub / step).to_integral_value(ROUND_HALF_UP)))
    return PriceCard(uid, step, step_rub, lot, future,
                     Decimal(str(meta.go_buy)) if getattr(meta, "go_buy", None) else None,
                     Decimal(str(meta.go_sell)) if getattr(meta, "go_sell", None) else None)


def with_margin(card: PriceCard, margin: dict) -> PriceCard:
    """ГО из ``GetFuturesMargin`` поверх карточки ядра; шаг и его стоимость брокера, если пришли."""
    return replace(card, go_buy=margin.get("go_buy") or card.go_buy, go_sell=margin.get("go_sell") or card.go_sell,
                   price_step=margin.get("price_step") or card.price_step,
                   step_rub=margin.get("step_rub") or card.step_rub)


def to_step(price: Decimal, step: Decimal, rounding: str) -> Decimal:
    """Цена на сетке шага; ``rounding`` — ``ROUND_FLOOR`` или ``ROUND_CEILING``."""
    if step <= 0:
        return price
    return ((price / step).to_integral_value(rounding) * step).quantize(step)


def entry_limit(card: PriceCard, price: Decimal, side: str) -> Decimal:
    """Лимит входа в безопасную сторону: покупка вниз, продажа вверх."""
    return to_step(price, card.price_step, ROUND_FLOOR if side == "BUY" else ROUND_CEILING)


def exit_limit(card: PriceCard, base: Decimal, side: str, offset_steps: int) -> Decimal:
    """Лимит выхода с запасом ``offset_steps`` шагов в сторону исполнения: продажа ниже, покупка выше."""
    shift = card.price_step * offset_steps
    if side == "SELL":
        return to_step(base - shift, card.price_step, ROUND_FLOOR)
    return to_step(base + shift, card.price_step, ROUND_CEILING)


def target_limit(card: PriceCard, price: Decimal, side: str) -> Decimal:
    """Лимит цели не хуже её цены: продажа вверх, покупка вниз."""
    return to_step(price, card.price_step, ROUND_CEILING if side == "SELL" else ROUND_FLOOR)


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


def trade_lots(card: PriceCard, quantities: list[int], lots_executed: int) -> list[int]:
    """Количество сделок в лотах: брокер может прислать штуки — тогда их сумма равна лотам × лот."""
    if card.lot > 1 and sum(quantities) == lots_executed * card.lot and all(q % card.lot == 0 for q in quantities):
        return [q // card.lot for q in quantities]
    return list(quantities)


def in_band(price: Decimal, band: tuple[Decimal, Decimal] | None) -> bool:
    return band is None or band[0] <= price <= band[1]
