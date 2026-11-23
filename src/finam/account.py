"""``account``: только чтение — счёт, заявки, сделки, транзакции, инструменты, цены, расписание, лимиты.

Проверки Ф0.2–Ф0.9 (add-finam-probe, решение 5). Заявки не ставятся и не снимаются.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta, timezone
from decimal import Decimal

from src.finam.checks import ERROR, NO, SKIPPED, YES, Run, interval, num, ts
from src.finam.instruments import FinamInstrument, InstrumentNameError, dec
from src.finam.orders import OrderDesk, describe, status_name
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2 as md_pb2
from src.finam.proto.grpc.tradeapi.v1.metrics import usage_metrics_service_pb2 as metrics_pb2
from src.sandbox.gateway import BrokerCallError

DEFAULT_INSTRUMENTS = ("Si", "BR", "NG", "GOLD", "SBER")
LIST_LIMIT = 20
HISTORY_LIMIT = 500


def money(value) -> str:
    amount = dec(value)
    if amount is None:
        return "нет"
    currency = getattr(value, "currency_code", "") or ""
    return f"{num(amount)} {currency}".strip()


def usage_metrics(gateway) -> str:
    response = gateway.call("metrics.GetUsageMetrics", metrics_pb2.GetUsageMetricsRequest())
    parts = [f"{quota.name}: {quota.remaining} из {quota.limit}, сброс {ts(quota.reset_time)}"
             for quota in response.quotas]
    return "; ".join(parts) or "квот нет"


class AccountRun(Run):
    TITLE = "Счёт Финама: только чтение"
    CHECKS = (
        ("Ф0.2", "Счёт"),
        ("Ф0.3", "Заявки"),
        ("Ф0.4", "Сделки"),
        ("Ф0.5", "Транзакции"),
        ("Ф0.6", "Инструменты"),
        ("Ф0.7", "Цены"),
        ("Ф0.8", "Расписание"),
        ("Ф0.9", "Лимиты"),
    )

    def __init__(self, *args, names=DEFAULT_INSTRUMENTS, days: int = 30, **kwargs):
        super().__init__(*args, **kwargs)
        self.names = list(names)
        self.days = days
        self.items: list[FinamInstrument] = []
        self.name_errors: dict[str, str] = {}
        self.account = None

    def _run(self) -> None:
        self._step("Ф0.6", self._check_instruments)
        self._step("Ф0.2", self._check_account)
        self._step("Ф0.3", self._check_orders)
        self._step("Ф0.4", self._check_trades)
        self._step("Ф0.5", self._check_transactions)
        if self.items:
            self._step("Ф0.7", self._check_prices)
            self._step("Ф0.8", self._check_schedule)
        else:
            self._skip(["Ф0.7", "Ф0.8"], "инструменты не найдены")
        self._step("Ф0.9", lambda: self._set("Ф0.9", YES, usage_metrics(self.gateway)) or True)

    # --- Ф0.2 ------------------------------------------------------------
    def _check_account(self) -> bool:
        account = self.gateway.call("accounts.GetAccount", accounts_pb2.GetAccountRequest(account_id=self.account_id))
        self.account = account
        portfolio = account.WhichOneof("portfolio")
        parts = [f"тип {account.type or 'нет'}", f"статус {account.status or 'нет'}",
                 f"equity {num(dec(account.equity))}", f"unrealized_profit {num(dec(account.unrealized_profit))}",
                 "cash " + (", ".join(money(item) for item in account.cash) or "нет")]
        if portfolio == "portfolio_mc":
            mc = account.portfolio_mc
            parts.append(f"портфель MC: available_cash {num(dec(mc.available_cash))}, initial_margin "
                         f"{num(dec(mc.initial_margin))}, maintenance_margin {num(dec(mc.maintenance_margin))}")
        elif portfolio == "portfolio_forts":
            forts = account.portfolio_forts
            parts.append(f"портфель FORTS: available_cash {num(dec(forts.available_cash))}, money_reserved "
                         f"{num(dec(forts.money_reserved))}")
        else:
            parts.append(f"портфель {portfolio or 'не указан'}")
        positions = [f"{self.instruments.name_of(item.symbol)} quantity {num(dec(item.quantity))}, average_price "
                     f"{num(dec(item.average_price))}, current_price {num(dec(item.current_price))}, "
                     f"maintenance_margin {num(dec(item.maintenance_margin))}" for item in account.positions]
        parts.append("позиции: " + ("; ".join(positions) if positions else "нет"))
        parts.append(f"открыт {ts(account.open_account_date)}")
        self._set("Ф0.2", YES, "; ".join(parts))
        return True

    # --- Ф0.3–Ф0.5 -------------------------------------------------------
    def _check_orders(self) -> bool:
        states = OrderDesk(self.gateway, self.account_id).orders()
        if not states:
            self._set("Ф0.3", YES, "заявок нет")
            return True
        times = [state.transact_at for state in states if state.HasField("transact_at")]
        oldest = min(times, key=lambda item: (item.seconds, item.nanos)) if times else None
        statuses = Counter(status_name(state.status) for state in states)
        shown = [describe(state, self.instruments) for state in states[:LIST_LIMIT]]
        more = f"; и ещё {len(states) - LIST_LIMIT}" if len(states) > LIST_LIMIT else ""
        self._set("Ф0.3", YES, f"{len(states)} заявок, самая ранняя {ts(oldest)}; по статусам "
                               f"{dict(statuses)}; " + "; ".join(shown) + more)
        return True

    def _period(self):
        now = self.timing.clock()
        return interval(now - timedelta(days=self.days), now)

    def _check_trades(self) -> bool:
        trades = list(self.gateway.call("accounts.Trades", accounts_pb2.TradesRequest(
            account_id=self.account_id, limit=HISTORY_LIMIT, interval=self._period())).trades)
        if not trades:
            self._set("Ф0.4", YES, f"сделок за {self.days} дн. нет")
            return True
        with_fee = sum(1 for trade in trades if trade.HasField("commission") and dec(trade.commission.value))
        shown = [f"{self.instruments.name_of(trade.symbol)} {ts(trade.timestamp)} цена {num(dec(trade.price))} "
                 f"{trade.currency or 'валюта пуста'}, размер {num(dec(trade.size))}, заявка "
                 f"{trade.order_id or 'нет'}, комиссия "
                 f"{num(dec(trade.commission.value)) if trade.HasField('commission') else 'нет'} "
                 f"{trade.commission.currency if trade.HasField('commission') else ''}".rstrip()
                 for trade in trades[:LIST_LIMIT]]
        self._set("Ф0.4", YES, f"{len(trades)} сделок за {self.days} дн., с комиссией {with_fee}; " + "; ".join(shown))
        return True

    def _check_transactions(self) -> bool:
        items = list(self.gateway.call("accounts.Transactions", accounts_pb2.TransactionsRequest(
            account_id=self.account_id, limit=HISTORY_LIMIT, interval=self._period())).transactions)
        if not items:
            self._set("Ф0.5", YES, f"транзакций за {self.days} дн. нет")
            return True
        categories = Counter(accounts_pb2.Transaction.TransactionCategory.Name(item.transaction_category)
                             for item in items)
        fees = [item for item in items
                if item.transaction_category == accounts_pb2.Transaction.TransactionCategory.COMMISSION]
        shown = [f"{ts(item.timestamp)} {self.instruments.name_of(item.symbol) if item.symbol else 'без инструмента'}"
                 f" «{item.transaction_name}» {money(item.change)}" for item in fees[:LIST_LIMIT]]
        trades = [item for item in items if item.HasField("trade")]
        self._set("Ф0.5", YES, f"{len(items)} транзакций за {self.days} дн.: {dict(categories)}; со сделкой "
                               f"{len(trades)}; комиссии: " + ("; ".join(shown) if shown else "нет"))
        return True

    # --- Ф0.6–Ф0.8 -------------------------------------------------------
    def _check_instruments(self) -> bool:
        lines, worst = [], YES
        for name in self.names:
            try:
                item = self.instruments.resolve(name)
            except InstrumentNameError as exc:
                self.name_errors[name] = str(exc)
                lines.append(str(exc))
                worst = NO
                continue
            except BrokerCallError as exc:
                lines.append(f"{name}: {exc}")
                worst = NO
                continue
            if not item.found:
                lines.append(f"{item.name}: символ в Assets не найден")
                worst = NO
                continue
            try:
                lines.append(self._compare(item))
                self.items.append(item)
            except BrokerCallError as exc:
                lines.append(f"{item.name}: {exc}")
                worst = NO
        self._set("Ф0.6", worst, "; ".join(lines) or "инструменты не заданы")
        return worst == YES

    def _compare(self, item: FinamInstrument) -> str:
        item.asset = self.gateway.call("assets.GetAsset", assets_pb2.GetAssetRequest(
            symbol=item.symbol, account_id=self.account_id))
        item.params = self.gateway.call("assets.GetAssetParams", assets_pb2.GetAssetParamsRequest(
            symbol=item.symbol, account_id=self.account_id))
        asset, params = item.asset, item.params
        head = f"{item.chosen + ' ; ' if item.chosen else ''}{item.name}: символ найден, площадка {item.mic}"
        if len(item.candidates) > 1:
            head += f" (кандидатов {len(item.candidates)})"
        finam = (f"Финам: тип {asset.type}, лот {num(item.lot_size)}, шаг {num(item.price_step)} (min_step "
                 f"{asset.min_step}, decimals {asset.decimals}), валюта {asset.quote_currency or 'нет'}")
        if asset.WhichOneof("asset_details") == "future_details":
            finam += (f", contract_size {num(dec(asset.future_details.contract_size))}, экспирация "
                      f"{ts(asset.future_details.expiration_date)}")
        tradable = params.is_tradable.value if params.HasField("is_tradable") else "нет"
        finam += (f"; торгуется {tradable}, лонг {accounts_status(params.longable)}, шорт "
                  f"{accounts_status(params.shortable)}, ГО лонг {money(params.long_initial_margin)}, ГО шорт "
                  f"{money(params.short_initial_margin)}, trade_lot_size {params.trade_lot_size}, риск-ставки "
                  f"{num(dec(params.long_risk_rate))}/{num(dec(params.short_risk_rate))}")
        card = item.tinvest
        if card is None:
            return f"{head}; {finam}; карточка Т-Инвест: не проверено (нет TINKOFF_TOKEN)"
        tinvest = f"Т-Инвест: лот {card.lot}, шаг {num(card.price_step)}"
        if card.step_cost is not None:
            tinvest += f", стоимость шага {num(card.step_cost)}"
        if item.tinvest_go_buy is not None:
            tinvest += f", ГО лонг {num(item.tinvest_go_buy)}, ГО шорт {num(item.tinvest_go_sell)}"
        lot_same = item.lot_size is not None and item.lot_size == Decimal(card.lot)
        step_same = item.price_step is not None and item.price_step == card.price_step
        verdict = (f"лот {'совпал' if lot_same else 'не совпал'}, шаг {'совпал' if step_same else 'не совпал'}, "
                   "стоимость шага — нет в API Финама")
        return f"{head}; {finam}; {tinvest}; {verdict}"

    def _check_prices(self) -> bool:
        lines = []
        for item in self.items:
            quote = self.gateway.call("marketdata.LastQuote", md_pb2.QuoteRequest(symbol=item.symbol)).quote
            book = self.gateway.call("marketdata.OrderBook", md_pb2.OrderBookRequest(symbol=item.symbol)).orderbook
            rows = []
            for row in list(book.rows)[:4]:
                side = row.WhichOneof("side")
                rows.append(f"{num(dec(row.price))} {'продажа' if side == 'sell_size' else 'покупка'} "
                            f"{num(dec(getattr(row, side)) if side else None)}")
            line = (f"{item.name}: last {num(dec(quote.last))}, bid {num(dec(quote.bid))}, ask {num(dec(quote.ask))}"
                    f" ({ts(quote.timestamp)}); стакан строк {len(book.rows)}: " + (", ".join(rows) or "пусто"))
            line += "; " + self._tinvest_scale(item, dec(quote.last))
            lines.append(line)
        self._set("Ф0.7", YES, "; ".join(lines))
        return True

    def _tinvest_scale(self, item: FinamInstrument, last: Decimal | None) -> str:
        if self.instruments.reference is None or item.tinvest is None:
            return "Т-Инвест: не проверено"
        response = self.instruments.reference.call("market_data.get_last_prices", instrument_id=[item.tinvest.uid])
        if not response.last_prices:
            return "Т-Инвест: последней цены нет"
        price = response.last_prices[0].price
        tinvest = Decimal(price.units) + Decimal(price.nano) / Decimal(10**9)
        if not last or not tinvest:
            return f"Т-Инвест: last {num(tinvest)}"
        ratio = last / tinvest
        scale = "одна шкала" if abs(ratio - 1) < Decimal("0.02") else f"отношение Финам/Т-Инвест {ratio:.4f}"
        return f"Т-Инвест: last {num(tinvest)} — {scale}"

    def _check_schedule(self) -> bool:
        clock = self.gateway.call("assets.Clock", assets_pb2.ClockRequest())
        lines = [f"время сервера {ts(clock.timestamp)}"]
        for item in self.items:
            schedule = self.gateway.call("assets.Schedule", assets_pb2.ScheduleRequest(symbol=item.symbol))
            sessions = []
            for session in schedule.sessions:
                start = session.interval.start_time.ToDatetime(tzinfo=timezone.utc)
                end = session.interval.end_time.ToDatetime(tzinfo=timezone.utc)
                sessions.append(f"{session.type} {start:%a %d.%m %H:%M}–{end:%H:%M} UTC")
            kinds = Counter(session.type for session in schedule.sessions)
            lines.append(f"{item.name}: сессий {len(sessions)}, типы {dict(kinds)}: " + "; ".join(sessions[:30]))
        self._set("Ф0.8", YES, "; ".join(lines))
        return True


def accounts_status(value) -> str:
    try:
        return type(value).Status.Name(value.value)
    except (AttributeError, ValueError):
        return str(getattr(value, "value", "нет"))


__all__ = ["AccountRun", "DEFAULT_INSTRUMENTS", "ERROR", "SKIPPED", "usage_metrics"]
