import csv
import shutil
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional

from src.broker.port import BrokerPort
from src.logging_setup import get_logger
from src.portfolio import (
    BrokerEvent,
    ContractMeta,
    OrderResult,
    OrderStatus,
    Position,
    PositionManager,
    ProtectiveOrder,
    Signal,
)
from src.trade_journal import (
    COLUMNS_RU,
    JournalEvent,
    OpType,
    TradeJournal,
    format_dt,
    parse_hhmm,
)

UTC = timezone.utc
_MSK = timezone(timedelta(hours=3))

log = get_logger(__name__)


@dataclass
class Order:
    """Живой отложенный ордер исполнения."""

    order_id: int
    position_id: str
    ticker: str
    side: str
    qty: int
    limit_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    timeframe: str
    ts_order: datetime
    deadline: datetime
    contract: str
    risk_pct: Optional[float] = None
    risk_rub: Optional[float] = None
    source: str = ""
    status: OrderStatus = OrderStatus.NEW


def _next_clearing_utc(now: datetime, clearing_times_msk: list[str]) -> datetime:
    """Ближайший момент клиринга (в UTC) для `now`."""
    offsets = sorted(parse_hhmm(item) for item in clearing_times_msk)
    msk_now = now.astimezone(_MSK)
    base = datetime(msk_now.year, msk_now.month, msk_now.day, tzinfo=_MSK)
    for off in offsets:
        cand = base + off
        if cand > msk_now:
            return cand.astimezone(UTC)
    return (base + timedelta(days=1) + offsets[0]).astimezone(UTC)


def _ttl_for(timeframe: str) -> Optional[int]:
    from src.scheduler.timing import tf_period_minutes

    if not timeframe:
        return None
    minutes = tf_period_minutes(timeframe)
    if minutes <= 30:
        return 3600
    if minutes < 240:
        return 4 * 3600
    return None  # живёт до ближайшего клиринга


def _fee(pnl_rub: float) -> float:
    return round(abs(pnl_rub) * 0.001, 2)


def _pnl(side: str, avg: float, exit_price: float, qty: int, contract: ContractMeta) -> float:
    if contract.price_step <= 0:
        return round((exit_price - avg) * qty * (-1 if side == "SELL" else 1), 2)
    delta = (exit_price - avg) / contract.price_step * contract.step_cost
    return round(delta * qty * (-1 if side == "SELL" else 1), 2)


def _notes(timeframe: str, over_risk: bool = False, extra: str = "", source: str = "") -> str:
    parts = [extra] if extra else []
    if timeframe:
        parts.append(f"tf={timeframe}")
    if source:
        parts.append(f"source={source}")
    if over_risk:
        parts.append("over_risk=true")
    return " ".join(parts)


class JournalBroker(BrokerPort):
    """Имитированное исполнение: состояния восстановливаются из журнала сделок.

    Все изменения пишутся в `TradeJournal` append-only. Позиции, защитные стопы,
    TTL и клиринг пересчитываются на каждой закрытой свече в `track_bar`.
    """

    def __init__(
        self,
        journal: TradeJournal,
        manager: PositionManager,
        clearing_times_msk: list[str],
        contract_names: Optional[Mapping[str, str]] = None,
    ):
        self.journal = journal
        self.manager = manager
        self.clearing_times_msk = list(clearing_times_msk)
        self._orders: dict[int, Order] = {}
        self._events: deque[BrokerEvent] = deque()
        self._last_clearing_check: Optional[datetime] = None
        self._contracts: dict[str, ContractMeta] = {}
        self._names: dict[str, str] = dict(contract_names or {})
        self.load_state()

    # ——— Порт ———

    def set_names(self, names: Optional[Mapping[str, str]]) -> None:
        """Отображение тикер -> короткое имя (NG-10.26) для пользовательских файлов/сообщений."""
        if names:
            self._names.update(names)

    def _display(self, ticker: str) -> str:
        """Короткое имя контракта для пользовательского вывода; тикер — только фолбэк."""
        return self._names.get(ticker) or ticker

    def load_state(self) -> None:
        self._orders = {}
        for order_id, restored in self.manager.pending.items():
            ts = restored.ts_order
            deadline = ts + timedelta(seconds=restored.ttl) if restored.ttl else _next_clearing_utc(ts, self.clearing_times_msk)
            self._orders[order_id] = Order(
                order_id=order_id,
                position_id=restored.position_id,
                ticker=restored.ticker,
                side=restored.side,
                qty=restored.qty,
                limit_price=restored.limit_price,
                stop_price=restored.stop_price,
                take_profit=restored.take_profit,
                timeframe=restored.timeframe,
                ts_order=ts,
                deadline=deadline,
                contract=restored.ticker,
                risk_pct=restored.risk_pct,
                risk_rub=restored.risk_rub,
            )
        for pos in self.manager.positions.values():
            ts = pos.ts_entry or datetime.now(UTC).replace(microsecond=0)
            pos.protective = self._protective_for(pos, ts)

    def contract_for(self, ticker: str) -> Optional[ContractMeta]:
        return self._contracts.get(ticker)

    def record_rejection(self, signal: Signal, reason: str, now: datetime) -> OrderResult:
        """Фиксация отклонённой заявки в журнале без размещения ордера.

        Используется на пути дедупликации входа («один ордер на бар»): повторный
        входящий сигнал за тот же бар записывается причиной `duplicate`,
        ордер не создаётся.
        """
        return self._reject(signal, reason, now)

    def set_contracts(self, contracts: Mapping[str, ContractMeta]) -> None:
        self._contracts.update(contracts)
        for pos in self.manager.positions.values():
            pos.ticker = pos.ticker  # noop, поле фиксировано при восстановлении

    def place_order(
        self,
        signal: Signal,
        contract: ContractMeta,
        now: datetime,
        reject_reason: str = "",
    ) -> OrderResult:
        ticker = signal.ticker
        self._contracts[ticker] = contract
        if signal.qty is None or signal.qty < 1:
            return self._reject(signal, reject_reason or "qty-negative", now)
        if self.manager.has_open_for(ticker):
            return self._reject(signal, "otherexisting", now)
        if not self.manager.margin_ok(signal.qty, contract, signal.side):
            return self._reject(signal, "margin", now)

        order_id = self.journal.next_id
        ttl = _ttl_for(signal.timeframe)
        deadline = now + timedelta(seconds=ttl) if ttl else _next_clearing_utc(now, self.clearing_times_msk)
        order = Order(
            order_id=order_id,
            position_id=signal.position_id,
            ticker=ticker,
            side=signal.side,
            qty=signal.qty,
            limit_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit=signal.take_profit,
            timeframe=signal.timeframe,
            ts_order=now,
            deadline=deadline,
            contract=ticker,
            risk_pct=signal.risk_pct,
            risk_rub=signal.risk_rub,
            source=signal.source,
        )
        row = self._order_row(order, contract, now)
        self.journal.append(row)
        self._orders[order_id] = order
        restored = self._restored(order)
        self.manager.register_order(order_id, restored)
        display = self._display(ticker)
        self._emit(
            "order",
            now,
            signal.position_id,
            f"Заявка {signal.side} {signal.qty} {display} по {signal.entry_price} принята (id={order_id})",
        )
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.NEW,
            position_id=signal.position_id,
            side=signal.side,
            qty=signal.qty,
            limit_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit=signal.take_profit,
            reason="",
            message=f"Заявка {signal.side} {signal.qty} {display} по {signal.entry_price} размещена (id={order_id})",
            ts_order=now,
        )

    def cancel_order(self, order_id: int, reason: str) -> OrderResult:
        order = self._orders.get(order_id)
        if order is None or order.status != OrderStatus.NEW:
            return self._noop_result(f"Заявка {order_id} не найдена или не активна")
        order.status = OrderStatus.CANCELLED
        self._write_terminal(order, reason, now := datetime.now(UTC).replace(microsecond=0))
        self._orders.pop(order_id, None)
        self.manager.drop_order(order_id)
        self._emit("cancel", now, order.position_id, f"Заявка {order_id} отменена ({reason})")
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            position_id=order.position_id,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            take_profit=order.take_profit,
            reason=reason,
            message=f"Заявка {order_id} отменена ({reason})",
            ts_order=now,
        )

    def track_bar(
        self,
        now: datetime,
        prices: Mapping[str, tuple[float, float, float]],
        contracts: Mapping[str, ContractMeta],
    ) -> list[OrderResult]:
        self.set_contracts(contracts)
        results: list[OrderResult] = []
        lows = {t: p[0] for t, p in prices.items()}
        highs = {t: p[1] for t, p in prices.items()}
        closes = {t: p[2] for t, p in prices.items()}

        cancel_pids = self.manager.track_bar(closes, dict(self._contracts))
        for pid in cancel_pids:
            order = next((o for o in self._orders.values() if o.position_id == pid and o.status == OrderStatus.NEW), None)
            if order is not None:
                results.append(self.cancel_order(order.order_id, "risk_cap"))

        for pos in list(self.manager.positions.values()):
            if pos.qty > 0 and pos.over_risk and pos.ticker in closes:
                results.append(self.close_position(pos, closes[pos.ticker], now, "over_risk"))

        for pos in list(self.manager.positions.values()):
            if pos.qty <= 0 or pos.ticker not in closes:
                continue
            low, high = lows.get(pos.ticker, closes[pos.ticker]), highs.get(pos.ticker, closes[pos.ticker])
            if pos.side == "BUY" and pos.stop_price is not None and low <= pos.stop_price:
                results.append(self.close_position(pos, pos.stop_price, now, "protective"))
            elif pos.side == "SELL" and pos.stop_price is not None and high >= pos.stop_price:
                results.append(self.close_position(pos, pos.stop_price, now, "protective"))
            elif pos.take_profit is not None and (
                (pos.side == "BUY" and high >= pos.take_profit) or (pos.side == "SELL" and low <= pos.take_profit)
            ):
                results.append(self.close_position(pos, pos.take_profit, now, "tp"))

        for order_id in list(self._orders):
            order = self._orders.get(order_id)
            if order is None or order.status != OrderStatus.NEW:
                continue
            if now >= order.deadline:
                results.append(self._expire_order(order_id, now))
                continue
            low, high = lows.get(order.ticker, closes.get(order.ticker, order.limit_price)), highs.get(order.ticker, closes.get(order.ticker, order.limit_price))
            reached = (order.side == "BUY" and low <= order.limit_price) or (order.side == "SELL" and high >= order.limit_price)
            if reached:
                results.extend(self._execute_entry(order, now))

        self._update_floating(closes)
        return results

    def _update_floating(self, closes: Mapping[str, float]) -> None:
        floating = 0.0
        for pos in self.manager.positions.values():
            if pos.qty <= 0 or pos.ticker not in closes:
                continue
            contract = self._contracts.get(pos.ticker)
            if contract is None:
                continue
            floating += _pnl(pos.side, pos.avg_price, closes[pos.ticker], pos.qty, contract)
        self.manager.account.set_floating_pnl(round(floating, 2))

    def run_clearing(self, now: datetime) -> list[OrderResult]:
        results: list[OrderResult] = []
        for order_id in list(self._orders):
            order = self._orders.get(order_id)
            if order is not None and order.status == OrderStatus.NEW:
                results.append(self.cancel_order(order_id, "clearing"))
        for pos in self.manager.positions.values():
            pos.protective = self._protective_for(pos, now)

        open_positions = [p for p in self.manager.positions.values() if p.qty > 0]
        self._append_snapshot(
            now,
            balance=self.manager.account.balance,
            realized=self.manager.account.realized_cycle,
            positions=len(open_positions),
            max_risk_pct=self.manager.max_risk_pct,
        )
        self.manager.account.clear()
        self._emit(
            "clear",
            now,
            "",
            f"Клиринг: снимок баланса {self.manager.account.balance:g} руб, открыто позиций: {len(open_positions)}",
        )
        return results

    def run_clearing_if_due(self, now: datetime) -> bool:
        if not self.clearing_times_msk:
            return False
        msk_now = now.astimezone(_MSK)
        today = datetime(msk_now.year, msk_now.month, msk_now.day, tzinfo=_MSK)
        instants = sorted((today + parse_hhmm(t) for t in self.clearing_times_msk))
        latest_passed = max((i for i in instants if i <= msk_now), default=None)
        if latest_passed is None:
            return False
        if self._last_clearing_check is not None and latest_passed.astimezone(UTC) <= self._last_clearing_check:
            return False
        self._last_clearing_check = latest_passed.astimezone(UTC)
        self.run_clearing(now)
        return True

    def drain_events(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    # ——— Внутренняя логика ———

    def _protective_for(self, pos: Position, ts: datetime) -> ProtectiveOrder:
        return ProtectiveOrder(
            position_id=pos.position_id,
            ticker=pos.ticker,
            side="SELL" if pos.side == "BUY" else "BUY",
            qty=pos.qty,
            stop_price=pos.stop_price if pos.stop_price else None,
            take_profit=pos.take_profit if pos.take_profit else None,
            ts_created=ts,
        )

    def _reject(self, signal: Signal, reason: str, now: datetime) -> OrderResult:
        display = self._display(signal.ticker)
        event = JournalEvent(
            id=self.journal.next_id,
            op=OpType.CANCEL.value,
            ts=format_dt(now),
            order_id="",
            position_id=signal.position_id,
            contract=display,
            side=signal.side,
            qty=str(signal.qty or 0),
            price=_fmt(signal.entry_price),
            stop=_fmt(signal.stop_price),
            pnl_part="",
            fee="",
            deposit="",
            risk_pct=_fmt(signal.risk_pct),
            risk_rub=_fmt(signal.risk_rub),
            go="",
            reason=reason,
            notes=_notes(signal.timeframe, source=signal.source),
        )
        self.journal.append(event)
        self._emit("order", now, signal.position_id, f"Сделка {signal.side} {display} отклонена ({reason})")
        return OrderResult(
            order_id=event.id,
            status=OrderStatus.CANCELLED,
            position_id=signal.position_id,
            side=signal.side,
            qty=signal.qty or 0,
            limit_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit=signal.take_profit,
            reason=reason,
            message=f"Сделка {signal.side} {display} отклонена ({reason})",
            ts_order=now,
        )

    def _order_row(self, order: Order, contract: ContractMeta, now: datetime) -> JournalEvent:
        """ЗАЯВКА: принятая, но не исполненная заявка (связь с ВХОД по order_id)."""
        return JournalEvent(
            id=order.order_id,
            op=OpType.ORDER.value,
            ts=format_dt(now),
            order_id=str(order.order_id),
            position_id=order.position_id,
            contract=self._display(order.ticker),
            side=order.side,
            qty=str(order.qty),
            price=_fmt(order.limit_price),
            stop=_fmt(order.stop_price),
            pnl_part="",
            fee="",
            deposit="",
            risk_pct=_fmt(order.risk_pct),
            risk_rub=_fmt(order.risk_rub),
            go=_fmt(self.manager.get_go(order.qty, contract, order.side)),
            reason="",
            notes=_notes(order.timeframe, source=order.source),
        )

    def _fill_row(
        self,
        order: Order,
        contract: ContractMeta,
        now: datetime,
        over_risk: bool = False,
        op: str = OpType.ENTRY.value,
    ) -> JournalEvent:
        """Исполнение заявки: ВХОД для новой позиции, ДОБОР — для добора."""
        return JournalEvent(
            id=self.journal.next_id,
            op=op,
            ts=format_dt(now),
            order_id=str(order.order_id),
            position_id=order.position_id,
            contract=self._display(order.ticker),
            side=order.side,
            qty=str(order.qty),
            price=_fmt(order.limit_price),
            stop=_fmt(order.stop_price),
            pnl_part="",
            fee="",
            deposit="",
            risk_pct=_fmt(order.risk_pct),
            risk_rub=_fmt(order.risk_rub),
            go=_fmt(self.manager.get_go(order.qty, contract, order.side)),
            reason="",
            notes=_notes(order.timeframe, over_risk=over_risk, source=order.source),
        )

    def _write_terminal(self, order: Order, reason: str, now: datetime) -> None:
        notes = _notes(order.timeframe, extra="expired" if reason == "ttl" else "", source=order.source)
        event = JournalEvent(
            id=self.journal.next_id,
            op=OpType.CANCEL.value,
            ts=format_dt(now),
            order_id=str(order.order_id),
            position_id=order.position_id,
            contract=self._display(order.ticker),
            side=order.side,
            qty=str(order.qty),
            price=_fmt(order.limit_price),
            stop=_fmt(order.stop_price),
            pnl_part="",
            fee="",
            deposit="",
            risk_pct=_fmt(order.risk_pct),
            risk_rub=_fmt(order.risk_rub),
            go="",
            reason=reason,
            notes=notes,
        )
        self.journal.append(event)

    def _expire_order(self, order_id: int, now: datetime) -> OrderResult:
        order = self._orders.get(order_id)
        if order is None:
            return self._noop_result("")
        order.status = OrderStatus.EXPIRED
        self._write_terminal(order, "ttl", now)
        self._orders.pop(order_id, None)
        self.manager.drop_order(order_id)
        self._emit("cancel", now, order.position_id, f"Заявка {order_id} истекла по TTL")
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.EXPIRED,
            position_id=order.position_id,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            take_profit=order.take_profit,
            reason="ttl",
            message=f"Заявка {order_id} истекла по TTL",
            ts_order=now,
        )

    def _execute_entry(self, order: Order, now: datetime) -> list[OrderResult]:
        contract = self._contracts.get(order.ticker)
        if contract is None:
            return [self._noop_result(f"Нет метаданных контракта для {self._display(order.ticker)}")]
        order.status = OrderStatus.FILLED
        over_risk = self._position_over_limit(order.position_id, order.qty, order.limit_price, contract)
        pos = self.manager.positions.get(order.position_id)
        is_add = pos is not None and pos.qty > 0
        if pos is None:
            pos = Position(
                position_id=order.position_id,
                ticker=order.ticker,
                side=order.side,
                qty=0,
                avg_price=order.limit_price,
                stop_price=order.stop_price,
                take_profit=order.take_profit,
                ts_entry=now,
                timeframe=order.timeframe,
            )
            self.manager.positions[order.position_id] = pos
        pos.average(order.limit_price, order.qty)
        if over_risk:
            pos.mark_over_risk()
        pos.protective = self._protective_for(pos, now)

        event = self._fill_row(
            order,
            contract,
            now,
            over_risk=over_risk,
            op=OpType.ADD.value if is_add else OpType.ENTRY.value,
        )
        self.journal.append(event)
        if is_add:
            self._emit("add", now, order.position_id, f"Добор {order.side} {order.qty} {self._display(order.ticker)} по {order.limit_price}")
        self._orders.pop(order.order_id, None)
        self.manager.drop_order(order.order_id)
        display = self._display(order.ticker)
        if not is_add:
            self._emit("fill", now, order.position_id, f"Вход {order.side} {order.qty} {display} по {order.limit_price}")
        if pos.protective is not None:
            self._emit("protective", now, order.position_id, f"Защитный стоп {order.stop_price} / ТП {order.take_profit} установлен")
        if over_risk:
            self._emit("over_risk", now, order.position_id, "Лимит перекоса достигнут — позиция закрывается контр-сделкой")

        result = OrderResult(
            order_id=order.order_id,
            status=OrderStatus.FILLED,
            position_id=order.position_id,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            take_profit=order.take_profit,
            reason="over_risk" if over_risk else "",
            message=f"Вход {order.side} {order.qty} {self._display(order.ticker)} по {order.limit_price}",
            ts_order=now,
        )
        results = [result]
        if over_risk:
            results.append(self.close_position(pos, order.limit_price, now, "over_risk"))
        return results

    def _position_over_limit(self, position_id: str, qty: int, price: float, contract: ContractMeta) -> bool:
        existing = self.manager.positions.get(position_id)
        total_qty = qty + (existing.qty if existing else 0)
        return contract.position_value(total_qty, price) > self.manager.over_risk_cap()

    def close_position(self, pos: Position, exit_price: float, now: datetime, reason: str) -> OrderResult:
        contract = self._contracts.get(pos.ticker)
        closed_qty = pos.qty
        pnl = _pnl(pos.side, pos.avg_price, exit_price, closed_qty, contract) if contract else 0.0
        fee = _fee(pnl)
        pnl -= fee
        self.manager.account.realize(pnl)
        event = JournalEvent(
            id=self.journal.next_id,
            op=OpType.EXIT.value if closed_qty >= pos.qty else OpType.TAKE.value,
            ts=format_dt(now),
            order_id="",
            position_id=pos.position_id,
            contract=self._display(pos.ticker),
            side="SELL" if pos.side == "BUY" else "BUY",
            qty=str(closed_qty),
            price=_fmt(exit_price),
            stop="",
            pnl_part=_fmt(pnl),
            fee=_fmt(fee),
            deposit="",
            risk_pct="",
            risk_rub="",
            go="",
            reason=reason,
            notes=_notes(pos.timeframe),
        )
        self.journal.append(event)
        display = self._display(pos.ticker)
        self._emit("fill", now, pos.position_id, f"Закрытие {closed_qty} {display} по {exit_price} (PnL {pnl:g})")
        if reason in ("protective", "over_risk", "signal", "reverse"):
            self._emit(
                reason,
                now,
                pos.position_id,
                f"Закрытие позиции {display} {closed_qty} шт: PnL {pnl:g} руб",
            )
        side = pos.side
        pos.reduce(closed_qty)
        if pos.qty == 0:
            self.manager.positions.pop(pos.position_id, None)
            self.manager.drop_order_stale(pos.position_id)
        return OrderResult(
            order_id=event.id,
            status=OrderStatus.FILLED,
            position_id=pos.position_id,
            side="SELL" if side == "BUY" else "BUY",
            qty=closed_qty,
            limit_price=None,
            stop_price=pos.stop_price,
            take_profit=pos.take_profit,
            reason=reason,
            message=f"Закрытие {display} по {exit_price}, PnL {pnl:g} руб",
            ts_order=now,
        )

    def _append_snapshot(
        self,
        now: datetime,
        balance: float,
        realized: float,
        positions: int,
        max_risk_pct: float,
    ) -> None:
        event = JournalEvent(
            id=self.journal.next_id,
            op=OpType.SNAPSHOT.value,
            ts=format_dt(now),
            order_id="",
            position_id="",
            contract="",
            side="",
            qty=str(positions),
            price="",
            stop="",
            pnl_part=_fmt(realized),
            fee="",
            deposit=_fmt(balance),
            risk_pct=_fmt(max_risk_pct),
            risk_rub="",
            go="",
            reason="clearing",
            notes=f"Клиринговый снимок: {positions} позиций",
        )
        self.journal.append(event)

    def _restored(self, order: Order):
        from src.trade_journal import RestoredOrder

        return RestoredOrder(
            order_id=order.order_id,
            position_id=order.position_id,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            stop_price=order.stop_price or 0.0,
            take_profit=order.take_profit,
            timeframe=order.timeframe,
            ts_order=order.ts_order,
            risk_pct=order.risk_pct,
            risk_rub=order.risk_rub,
            ttl=_ttl_for(order.timeframe),
        )

    def _emit(self, etype: str, now: datetime, position_id: str, message: str) -> None:
        self._events.append(BrokerEvent(type=etype, ts=now, position_id=position_id, message=message))

    def _noop_result(self, message: str) -> OrderResult:
        now = datetime.now(UTC).replace(microsecond=0)
        return OrderResult(
            order_id=0,
            status=OrderStatus.NONE,
            position_id="",
            side="",
            qty=0,
            limit_price=None,
            stop_price=None,
            take_profit=None,
            reason="",
            message=message or "",
            ts_order=now,
        )


def _fmt(value: Optional[float]) -> str:
    return "" if value is None else repr(round(value, 6))


_OP_BY_LEGACY_STATUS: dict[str, tuple[str, ...]] = {
    "NEW": (OpType.ORDER.value,),
    "FILLED": (OpType.ENTRY.value, OpType.ADD.value, OpType.TAKE.value, OpType.EXIT.value),
    "CLEARING": (OpType.SNAPSHOT.value,),
    "CANCELLED": (OpType.CANCEL.value,),
}


def filter_rows(journal: TradeJournal, op_or_status: str) -> list[JournalEvent]:
    """Выборка строк журнала по операции («ВХОД», «ОТМЕНА»…) или легаси-статусу (NEW/FILLED/…)."""
    ops = _OP_BY_LEGACY_STATUS.get(op_or_status, (op_or_status,))
    return [e for e in journal.events() if e.op in ops]


def count_status_rows(journal: TradeJournal, op_or_status: str) -> int:
    return len(filter_rows(journal, op_or_status))


def _migrate_legacy_journal(path: Path) -> None:
    """При наличии легаси-журнала (ограниченная схема) — резервная копия .bak.

    Старые строки не переносятся: журнал начинается с чистого файла, а состояние
    восстанавливается из резервной копии вручную при необходимости.
    """
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), [])
    except Exception:  # битый файл — не мешаем старту
        log.warning("Легаси-журнал %s не читается, пропускаю миграцию", path)
        return
    if header == COLUMNS_RU:
        return
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    log.warning("Легаси-журнал %s сохранён как %s", path, backup)


def create_journal_broker(
    journal_file: str,
    initial_deposit: float,
    max_risk_pct: float,
    clearing_times_msk: list[str],
    positions_file: Optional[str] = None,
    contract_names: Optional[Mapping[str, str]] = None,
) -> JournalBroker:
    """Сборка симулятора из конфига: журнал + восстановление состояния портфеля."""
    from src.config_loader import app_dir
    from src.portfolio import PositionManager

    path = Path(app_dir()) / journal_file
    _migrate_legacy_journal(path)
    positions_path = Path(app_dir()) / positions_file if positions_file else None
    journal = TradeJournal.created_on_init(path, positions_path=positions_path)
    state = journal.replay(initial_deposit=initial_deposit)
    manager = PositionManager(state, initial_deposit, max_risk_pct)
    return JournalBroker(journal, manager, clearing_times_msk, contract_names=contract_names)