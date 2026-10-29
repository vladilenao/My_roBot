"""``TInvestBroker`` — исполнение команд ``TradeManager`` настоящими заявками (add-tinvest-orders).

``submit`` отвечает сразу: ``ACK`` (принята или судьба неизвестна) либо
``REJECT``. Исполнения, подтверждённые снятия и поздние отказы приходят позже:
поток стрима пишет их в журнал заявок (``ingest``), главный поток превращает в
``ExecutionEvent`` (``drain_addressed_events``) и проводит через ``consume``.

Выходы — лимитом (design.md, решение 6): цели стоят у брокера сразу после
входа; стоп — стоп-лимит: условие по закрытой минуте проверяет контур, лимит
с запасом перевыставляется от последней цены, после исчерпания попыток —
аварийный рынок. Сигнальные выходы и закрытие идут тем же циклом.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping

from src.broker.journal_broker import JournalBroker, stop_triggered
from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus, FeeSource
from src.broker_contour.api import OrderFailure, OrderReport
from src.broker_contour.orders import (ACCEPTED, CANCELLED, CANCELLING, FILLED, PARTIAL, REJECTED, SENDING, UNKNOWN,
                                       LedgerExecution, LedgerOrder, OrderLedger, order_key)
from src.broker_contour.prices import (PriceCard, card_from_meta, entry_limit, exit_limit, in_band, target_limit,
                                       trade_lots, trade_price, with_margin)
from src.events.event import Event
from src.events.types import EventType
from src.logging_setup import get_logger
from src.trade_management.actions import (AddToTrade, CancelEntry, CloseTrade, EntryOrderType, MoveStop, OpenTrade,
                                          ReduceTrade, TradeAction)
from src.trade_management.models import TradePlan, rebase_on_average

log = get_logger(__name__)

_STATE = {"new": ACCEPTED, "partial": PARTIAL, "fill": FILLED, "cancelled": CANCELLED, "rejected": REJECTED}
ENTRY_KINDS = ("entry", "add")
EXIT_KINDS = ("exit", "stop", "emergency")
CANCEL_CONFIRM_SECS = 10
SYNC_EVERY_SECS = 60
MARGIN_EVERY = timedelta(days=1)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class ExitPolicy:
    offset_steps: int = 5
    requote_secs: int = 15
    attempts: int = 3
    retry_secs: int = 60


@dataclass
class TradeView:
    """Сделка глазами брокера: план, подтверждённый стоп, исполненные лоты и цели — только по фактам."""

    plan: TradePlan
    original_plan: TradePlan = field(init=False)
    entry_quantity: int = 0
    position: int = 0
    average: Decimal | None = None
    confirmed_stop: Decimal | None = None
    target_filled: dict[str, int] = field(default_factory=dict)
    cancel_command: str | None = None
    cancel_reason: str = ""
    entry_commands: dict[str, int] = field(default_factory=dict)   # команда входа → её лоты

    def __post_init__(self) -> None:
        self.original_plan = self.plan
        if not self.target_filled:
            self.target_filled = {target.target_id: 0 for target in self.plan.targets}

    @property
    def exit_side(self) -> str:
        return "SELL" if self.plan.side == "BUY" else "BUY"


@dataclass
class ExitJob:
    """Выход сделки циклом «лимит с запасом → перевыставление → аварийный рынок»."""

    job_id: str
    trade_id: str
    kind: str                         # exit — команда TradeManager · stop — условие стопа
    quantity: int | None              # None — весь остаток
    command_id: str | None = None
    base: Decimal | None = None       # цена первой заявки (стоп); дальше — последняя цена
    reference: Decimal | None = None
    reason: str = ""
    attempt: int = 0
    executed: int = 0
    retry_after: datetime | None = None
    alarmed: bool = False
    rejected_reported: bool = False


class TInvestBroker(BrokerPort):
    def __init__(self, api, ledger: OrderLedger, *, contour: str, policy: ExitPolicy = ExitPolicy(),
                 publish: Callable[[Event], None] = lambda event: None,
                 journal_has: Callable[[str], bool] = lambda execution_id: False,
                 entry_ttl: Callable[[str], int] = lambda timeframe: 3600,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._api = api
        self._ledger = ledger
        ledger.clock = clock
        self._contour = contour
        self._policy = policy
        self._publish = publish
        self._journal_has = journal_has
        self._entry_ttl = entry_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._views: dict[str, TradeView] = {}
        self._resumed: set[str] = set()
        self._jobs: dict[str, ExitJob] = {}          # trade_id → активный выход
        self._queued: dict[str, list[ExitJob]] = {}  # trade_id → выходы, ждущие активного
        self._events: list[ExecutionEvent] = []
        self._emitted: set[str] = set()
        self._contracts: dict = {}
        self._cards: dict[str, PriceCard] = {}
        self._margins: dict[str, dict] = {}
        self._margin_at: dict[str, datetime] = {}
        self._names: dict[str, str] = {}
        self._keys: dict[str, str] = {}              # UID брокера → ключ инструмента контура (тикер или UID)
        self._last_close: dict[str, Decimal] = {}
        self._processed_bars: set[tuple[str, datetime]] = set()
        self._sync: set[str] = set()
        self._target_retry: dict[str, datetime] = {}
        self._last_sync: datetime | None = None
        self.fills_since_capital = False
        self.stream_link = "ok"

    # --- карточки ---------------------------------------------------------
    def set_contracts(self, contracts: Mapping[str, object]) -> None:
        with self._lock:
            for uid, meta in contracts.items():
                self._contracts[uid] = meta
                self._cards[uid] = with_margin(card_from_meta(uid, meta), self._margins.get(uid, {}))

    def set_names(self, names: Mapping[str, str] | None) -> None:
        self._names.update(names or {})

    def contract_for(self, uid: str):
        """Карточка для допуска и бюджета ``TradeManager``: ГО — из ``GetFuturesMargin``, если прочитано."""
        meta = self._contracts.get(uid)
        margin = self._margins.get(uid)
        if meta is None or not margin:
            return meta
        return replace(meta, go_buy=float(margin["go_buy"]), go_sell=float(margin["go_sell"]))

    def refresh_margins(self, now: datetime) -> list[str]:
        """ГО фьючерсов из ``GetFuturesMargin``: при допуске инструмента и раз в сутки."""
        updated = []
        for uid in list(self._cards):
            card = self._card_for(uid)
            if card is None or not card.future or (uid in self._margin_at and now - self._margin_at[uid] < MARGIN_EVERY):
                continue
            try:
                margin = self._api.futures_margin(card.uid)
            except OrderFailure as failure:
                log.warning("ГО %s не прочитано: %s", self._display(uid), failure.kind)
                continue
            with self._lock:
                self._margins[uid] = margin
                self._margin_at[uid] = now
                self._cards[uid] = with_margin(card, margin)
            updated.append(uid)
        return updated

    def _display(self, uid: str) -> str:
        key = self._keys.get(uid, uid)
        return self._names.get(key, key)

    def _card_for(self, key: str) -> PriceCard | None:
        """Карточка инструмента с UID брокера: ключ эмуляции бывает тикером, а заявке нужен UID."""
        card = self._cards.get(key)
        if card is None or card.uid in self._keys:
            return card
        if _UUID.fullmatch(key):
            uid = key
        else:
            meta = self._contracts.get(key)
            try:
                uid = self._api.instrument_uid(getattr(meta, "ticker", None) or key, card.future)
            except OrderFailure as failure:
                log.warning("UID %s не определён: %s", self._display(key), failure.kind)
                return None
        card = replace(card, uid=uid)
        self._cards[key] = card
        self._keys[uid] = key
        return card

    def _card_by_uid(self, uid: str) -> PriceCard:
        return self._cards.get(self._keys.get(uid, uid)) or PriceCard(uid, Decimal(0), Decimal(0))

    def key_for(self, uid: str) -> str:
        return self._keys.get(uid, uid)

    # --- сделки -----------------------------------------------------------
    def register_trade(self, plan: TradePlan) -> None:
        with self._lock:
            self._views.setdefault(plan.trade_id, TradeView(plan))

    def resume_trade(self, recovered) -> None:
        """Состояние сделки из журнала после рестарта; выходы в работе — из журнала заявок и outbox."""
        plan, state = getattr(recovered, "plan", None), getattr(recovered, "state", None)
        if plan is None or state is None:
            return
        with self._lock:
            # TradeManager.restore зовётся часто; живое представление берётся из журнала один раз за процесс,
            # дальше его ведут только исполнения брокера.
            if plan.trade_id in self._resumed:
                return
            self._resumed.add(plan.trade_id)
            view = self._views.setdefault(plan.trade_id, TradeView(plan))
            if view.position or view.entry_quantity:
                return
            view.position = state.quantity
            view.entry_quantity = getattr(recovered, "entry_quantity", None) or state.quantity
            view.average = state.average_price
            if state.quantity > 0 and state.average_price is not None:
                view.plan = rebase_on_average(plan, state.average_price)
                view.plan = replace(view.plan, targets=tuple(
                    replace(t, price=state.target_prices.get(t.target_id, t.price)) for t in view.plan.targets))
                view.confirmed_stop = state.confirmed_stop or plan.stop_price
            for target_id, filled in (getattr(recovered, "target_filled", None) or {}).items():
                view.target_filled[target_id] = filled
            for pending in getattr(recovered, "pending_increases", ()):
                view.entry_commands[pending.action.command_id] = pending.action.quantity
            for pending in getattr(recovered, "pending_exits", ()):
                self._start_exit(view, pending.action, pending.submitted_at, send=False)
            rows = self._ledger.orders(trade_id=plan.trade_id, kinds=EXIT_KINDS)
            for row in rows:
                if not row.live or plan.trade_id in self._jobs:
                    continue
                stop = ":pv:stop:" in row.job
                self._jobs[plan.trade_id] = ExitJob(row.job, plan.trade_id, "stop" if stop else "exit", None,
                                                    None if stop else row.job, reason="protective" if stop else "exit")
            for job in (self._jobs.get(plan.trade_id), *self._queued.get(plan.trade_id, ())):
                if job is not None:
                    mine = [row for row in rows if row.job == job.job_id]
                    job.attempt = len(mine)
                    job.executed = sum(e.lots for row in mine for e in self._ledger.executions(row.key))

    def trade_state(self, trade_id: str) -> TradeView | None:
        return self._views.get(trade_id)

    def has_recovery_guard_close(self, action: TradeAction) -> bool:
        return False

    # --- порт -------------------------------------------------------------
    def submit(self, action: TradeAction, now: datetime) -> ExecutionEvent:
        with self._lock:
            view = self._views.get(action.trade_id)
            if view is None:
                return self._outcome(action, now, ExecutionStatus.REJECT, "unknown-trade")
            if isinstance(action, (OpenTrade, AddToTrade)):
                return self._submit_entry(view, action, now)
            if isinstance(action, MoveStop):
                if view.position <= 0:
                    return self._outcome(action, now, ExecutionStatus.REJECT, "trade-not-open")
                view.confirmed_stop = action.stop_price
                return replace(self._outcome(action, now, ExecutionStatus.ACK, action.reason),
                               execution_id=f"{action.command_id}:stop-active:{now.isoformat()}")
            if isinstance(action, CancelEntry):
                return self._submit_cancel(view, action, now)
            if isinstance(action, (CloseTrade, ReduceTrade)):
                if isinstance(action, ReduceTrade) and action.target_id is not None:
                    return self._outcome(action, now, ExecutionStatus.REJECT, "targets-at-broker")
                if view.position <= 0:
                    return self._outcome(action, now, ExecutionStatus.REJECT, "trade-not-open")
                self._start_exit(view, action, now)
                return self._outcome(action, now, ExecutionStatus.ACK, "exit-accepted")
            return self._outcome(action, now, ExecutionStatus.REJECT, "unsupported-command")

    def _submit_entry(self, view: TradeView, action, now: datetime) -> ExecutionEvent:
        if action.order_type is not EntryOrderType.LIMIT:
            return self._outcome(action, now, ExecutionStatus.REJECT, "limit-entry-required")
        key = order_key(action.command_id)
        existing = self._ledger.get(key)
        if existing is not None:
            return self._entry_answer(action, existing, now)
        card = self._card_for(view.plan.instrument_id)
        if card is None:
            return self._outcome(action, now, ExecutionStatus.REJECT, "no-contract-card")
        price = entry_limit(card, action.limit_price, view.plan.side)
        try:
            band = self._api.price_band(card.uid)
        except OrderFailure:
            band = None  # коридор не прочитан — проверку делает биржа
        if not in_band(price, band):
            return self._outcome(action, now, ExecutionStatus.REJECT, "price-out-of-band")
        view.entry_commands[action.command_id] = action.quantity
        row = LedgerOrder(key, action.command_id, view.plan.trade_id, "entry" if isinstance(action, OpenTrade) else "add",
                          card.uid, view.plan.side, "limit", action.quantity, price, job=action.command_id)
        row = self._send(row, now)
        return self._entry_answer(action, row, now)

    def _entry_answer(self, action, row: LedgerOrder, now: datetime) -> ExecutionEvent:
        if row.state == REJECTED:
            self._ledger.update(row.key, reported=1)
            return self._outcome(action, now, ExecutionStatus.REJECT, row.reason or "broker-rejected")
        reason = "broker-unknown" if row.state in (UNKNOWN, SENDING) else "accepted"
        return replace(self._outcome(action, now, ExecutionStatus.ACK, reason),
                       order_id=row.broker_order_id or action.command_id)

    def _submit_cancel(self, view: TradeView, action: CancelEntry, now: datetime) -> ExecutionEvent:
        entries = [row for row in self._ledger.orders(trade_id=view.plan.trade_id, live=True, kinds=ENTRY_KINDS)]
        if not entries:
            if view.position > 0:
                return self._outcome(action, now, ExecutionStatus.REJECT, "trade-already-open")
            return self._outcome(action, now, ExecutionStatus.CANCEL, action.reason)
        view.cancel_command, view.cancel_reason = action.command_id, action.reason
        for row in entries:
            self._request_cancel(row, now, notify=True)
        return self._outcome(action, now, ExecutionStatus.ACK, "cancelling")

    # --- отправка и снятие --------------------------------------------------
    def _send(self, row: LedgerOrder, now: datetime, *, resend: bool = False) -> LedgerOrder:
        """Строка журнала до вызова, затем ``PostOrder``; ошибки по виду из ``api.classify``."""
        if resend:
            row = self._ledger.update(row.key, state=SENDING, sends=row.sends + 1)
            self._notify("resent", row)
        else:
            row = self._ledger.create(row)
            self._notify("sent", row)
        try:
            if row.order_type == "market":
                report = self._api.post_market(row.instrument_uid, row.side, row.lots, row.key)
            else:
                report = self._api.post_limit(row.instrument_uid, row.side, row.lots, row.price, row.key)
        except OrderFailure as failure:
            return self._send_failed(row, failure, now)
        self._apply_report(row, replace(report, key=row.key), now)
        row = self._ledger.get(row.key)
        if row.kind in ENTRY_KINDS and row.state in (ACCEPTED, PARTIAL, FILLED):
            self._publish(Event.broker_event(EventType.ORDER_ACCEPTED, trade_id=row.trade_id,
                                             instrument=self._display(row.instrument_uid), bar_time=now,
                                             order_id=row.broker_order_id, status="ack", reason="accepted"))
        return row

    def _send_failed(self, row: LedgerOrder, failure: OrderFailure, now: datetime) -> LedgerOrder:
        if failure.kind == "duplicate":
            row = self._ledger.update(row.key, state=UNKNOWN, reason="duplicate")
            self._resolve(row, now)
            return self._ledger.get(row.key)
        if failure.kind == "unknown":
            log.warning("Заявка %s %s: ответ брокера потерян (%s)", row.kind, self._display(row.instrument_uid),
                        failure.code)
            row = self._ledger.update(row.key, state=UNKNOWN, reason=failure.code)
            self._notify("unknown", row)
            return row
        reason = "rate-limited" if failure.kind == "rate_limited" else failure.reason
        log.warning("Заявка %s %s отклонена: %s", row.kind, self._display(row.instrument_uid), failure)
        return self._ledger.update(row.key, state=REJECTED, reason=reason)

    def _request_cancel(self, row: LedgerOrder, now: datetime, *, notify: bool = False) -> None:
        if row.terminal:
            return
        if row.state in (UNKNOWN, SENDING) or not row.broker_order_id:
            self._ledger.update(row.key, cancel_requested=1)  # снимется после поиска по ключу
            return
        if row.state == CANCELLING:
            return
        try:
            self._api.cancel(row.broker_order_id)
        except OrderFailure as failure:
            log.warning("Снятие заявки %s %s: %s — уточняем по ключу", row.kind, self._display(row.instrument_uid),
                        failure.kind)
            self._ledger.update(row.key, cancel_requested=1)
            self._sync.add(row.key)
            return
        row = self._ledger.update(row.key, state=CANCELLING, cancel_requested=1)
        if notify:
            self._notify("cancelling", row)

    # --- отчёты брокера -----------------------------------------------------
    def ingest(self, report: OrderReport) -> bool:
        """Отчёт стрима (поток стрима): строка журнала по ключу или по номеру брокера; чужой — ``False``."""
        with self._lock:
            row = (self._ledger.get(report.key) if report.key else None) or (
                self._ledger.by_broker_id(report.broker_order_id) if report.broker_order_id else None)
            if row is None:
                return False
            self._apply_report(row, report, self._clock())
            return True

    def _apply_report(self, row: LedgerOrder, report: OrderReport, now: datetime) -> None:
        card = self._card_by_uid(row.instrument_uid)
        known = self._ledger.executions(row.key)
        if report.trades:
            lots = trade_lots(card, [*(e.lots for e in known), *(t.quantity for t in report.trades)],
                              report.lots_executed)[len(known):]
            for trade, quantity in zip(report.trades, lots):
                self._ledger.add_execution(LedgerExecution(f"tinvest:{trade.trade_id}", row.key, quantity,
                                                           trade_price(card, trade.price, trade.in_rub), trade.at))
        executions = self._ledger.executions(row.key)
        executed = sum(e.lots for e in executions)
        if report.lots_executed > executed:
            self._sync.add(row.key)   # сделки не пришли в этом отчёте — добрать из GetOrderState.stages
        values: dict = {"lots_executed": executed}
        if executed:
            values["average"] = (sum(e.price * e.lots for e in executions) / executed).quantize(
                card.price_step if card.price_step > 0 else Decimal("0.000001"))
        if report.broker_order_id and (not report.temporary or not row.broker_order_id):
            values["broker_order_id"] = report.broker_order_id
        state = _STATE.get(report.status)
        if state is not None and not row.terminal:
            if row.state == CANCELLING and state in (ACCEPTED, PARTIAL):
                state = CANCELLING
            if state == FILLED and executed < row.lots:
                state = PARTIAL       # статус «исполнена» без всех сделок — ждём их
            values["state"] = state
        self._ledger.update(row.key, **values)

    # --- неизвестные заявки и досверка ----------------------------------------
    def _resolve(self, row: LedgerOrder, now: datetime) -> None:
        """Поиск по ключу: найдена — её состояние; пусто — повтор тем же ключом или итог «не дошла»."""
        try:
            report = self._api.order_by_key(row.key)
        except OrderFailure as failure:
            log.warning("Поиск заявки по ключу не удался: %s", failure.kind)
            return
        if report is not None:
            self._apply_report(row, report, now)
            row = self._ledger.get(row.key)
            self._notify("found", row, state=row.state)
            if row.cancel_requested and row.live:
                self._request_cancel(row, now)
            return
        if row.reason == "duplicate":
            return  # брокер назвал ключ занятым, а поиск пуст — спросим снова на следующем шаге
        if self._actual(row, now):
            self._send(row, now, resend=True)
        else:
            self._ledger.update(row.key, state=CANCELLED, reason="entry-undelivered")

    def _actual(self, row: LedgerOrder, now: datetime) -> bool:
        view = self._views.get(row.trade_id)
        if view is None or row.cancel_requested:
            return False
        if row.kind in ENTRY_KINDS:
            created = datetime.fromisoformat(row.created_at)
            return view.cancel_command is None and now - created < timedelta(seconds=self._entry_ttl(view.plan.timeframe))
        job = self._jobs.get(row.trade_id)
        return view.position > 0 and (row.kind == "target" or (job is not None and job.job_id == row.job))

    def recover(self, now: datetime) -> None:
        """До первого тика: «отправлена» без ответа — «неизвестно», затем поиск по ключу всего незавершённого."""
        with self._lock:
            for row in self._ledger.orders(live=True):
                if row.state == SENDING:
                    row = self._ledger.update(row.key, state=UNKNOWN)
                self._sync.add(row.key)
            self._synchronize(now, force=True)

    def _synchronize(self, now: datetime, *, force: bool = False) -> None:
        due = force or self._last_sync is None or (now - self._last_sync).total_seconds() >= SYNC_EVERY_SECS
        for row in self._ledger.orders(live=True):
            stale_cancel = row.state == CANCELLING and (
                now - datetime.fromisoformat(row.updated_at)).total_seconds() >= CANCEL_CONFIRM_SECS
            if row.state == UNKNOWN or row.key in self._sync or stale_cancel or due:
                self._sync.discard(row.key)
                if row.state == UNKNOWN:
                    self._resolve(row, now)
                else:
                    self._refresh(row, now)
        for key in list(self._sync):     # завершённые заявки с недостающими сделками
            self._sync.discard(key)
            row = self._ledger.get(key)
            if row is not None:
                self._refresh(row, now)
        if due:
            self._last_sync = now

    def resync(self, now: datetime | None = None) -> None:
        """После переподключения стрима: досверить всё незавершённое по ключу."""
        with self._lock:
            self._synchronize(now or self._clock(), force=True)

    def _refresh(self, row: LedgerOrder, now: datetime) -> None:
        try:
            report = self._api.order_by_key(row.key)
        except OrderFailure as failure:
            log.warning("Состояние заявки не прочитано: %s", failure.kind)
            self._sync.add(row.key)
            return
        if report is None:
            return
        self._apply_report(row, report, now)
        executed = sum(e.lots for e in self._ledger.executions(row.key))
        if report.terminal and report.lots_executed > executed and not report.trades and report.executed_rub:
            # Брокер сообщил исполнение без сделок биржи и в stages: остаток проводится по средней заявки.
            card = self._card_by_uid(row.instrument_uid)
            self._ledger.add_execution(LedgerExecution(
                f"tinvest-order:{report.broker_order_id}:{report.lots_executed}", row.key,
                report.lots_executed - executed, trade_price(card, report.executed_rub, card.future), now))
            self._apply_report(self._ledger.get(row.key), replace(report, trades=()), now)

    # --- обслуживание: выходы, цели, итоги ------------------------------------
    def service(self, now: datetime) -> None:
        """Граница тика или опрос: досверка, выходы, цели, повторы отказов."""
        with self._lock:
            self._synchronize(now)
            for row in self._ledger.orders(live=True):
                if row.cancel_requested and row.state in (ACCEPTED, PARTIAL):
                    self._request_cancel(row, now)
            for trade_id in list(self._jobs):
                self._pump(trade_id, now)
            for view in list(self._views.values()):
                self._ensure_targets(view, now)

    def track_bar(self, now: datetime, ohlc: Mapping[str, tuple], contracts=None, *, bar_times=None) -> None:
        """Закрытая минута ядра: последняя цена инструмента и условие стопа открытых сделок."""
        with self._lock:
            for uid, bar in ohlc.items():
                moment = (bar_times or {}).get(uid, now)
                if (uid, moment) in self._processed_bars:
                    continue
                self._processed_bars.add((uid, moment))
                self._last_close[uid] = Decimal(str(bar[3]))
                for view in self._views.values():
                    if view.plan.instrument_id != uid or view.position <= 0 or view.confirmed_stop is None:
                        continue
                    job = self._jobs.get(view.plan.trade_id)
                    if job is not None and job.kind == "stop":
                        continue
                    triggered, _ = stop_triggered(view.plan.side, view.confirmed_stop, bar)
                    if triggered:
                        log.info("Стоп %s %s по минуте %s: выход стоп-лимитом", view.plan.trade_id,
                                 self._display(uid), moment)
                        self._start_stop(view, moment)
            self.service(self._clock())

    def _start_stop(self, view: TradeView, moment: datetime) -> None:
        trade_id = view.plan.trade_id
        job = ExitJob(f"{trade_id}:pv:stop:{moment.isoformat()}", trade_id, "stop", None, base=view.confirmed_stop,
                      reference=view.confirmed_stop, reason="protective")
        current = self._jobs.get(trade_id)
        if current is not None:   # стоп важнее сигнального выхода: тот ждёт и потом проверит остаток
            self._queued.setdefault(trade_id, []).insert(0, current)
            for row in self._ledger.orders(trade_id=trade_id, live=True, kinds=EXIT_KINDS):
                self._request_cancel(row, moment)
        self._jobs[trade_id] = job
        for row in self._ledger.orders(trade_id=trade_id, live=True, kinds=("target",)):
            self._request_cancel(row, moment)

    def _start_exit(self, view: TradeView, action, now: datetime, *, send: bool = True) -> None:
        trade_id = view.plan.trade_id
        quantity = None if isinstance(action, CloseTrade) else action.quantity
        job = ExitJob(action.command_id, trade_id, "exit", quantity, action.command_id,
                      reference=getattr(action, "reference_price", None), reason=action.reason)
        if any(j.job_id == job.job_id for j in (self._jobs.get(trade_id), *self._queued.get(trade_id, ())) if j):
            return
        if trade_id in self._jobs:
            self._queued.setdefault(trade_id, []).append(job)
            return
        self._jobs[trade_id] = job
        if quantity is None:
            for row in self._ledger.orders(trade_id=trade_id, live=True, kinds=("target",)):
                self._request_cancel(row, now)
        else:
            self._free_targets(view, quantity, now)
        if send:
            self._pump(trade_id, now)

    def _free_targets(self, view: TradeView, quantity: int, now: datetime) -> None:
        """Сокращение берёт свободные лоты, затем уменьшает заявку самой дальней цели."""
        targets = self._ledger.orders(trade_id=view.plan.trade_id, live=True, kinds=("target",))
        free = view.position - sum(row.remaining for row in targets)
        distance = {t.target_id: abs(t.price - (view.average or t.price)) for t in view.plan.targets}
        for row in sorted(targets, key=lambda r: distance.get(r.target_id, Decimal(0)), reverse=True):
            if free >= quantity:
                break
            self._request_cancel(row, now)
            free += row.remaining

    def _pump(self, trade_id: str, now: datetime) -> None:
        job, view = self._jobs.get(trade_id), self._views.get(trade_id)
        if job is None or view is None:
            return
        live = self._ledger.orders(trade_id=trade_id, live=True, kinds=EXIT_KINDS)
        live = [row for row in live if row.job == job.job_id]
        wanted = view.position if job.quantity is None else min(view.position, job.quantity - job.executed)
        if wanted <= 0:
            for row in live:
                self._request_cancel(row, now)
            if not live:
                self._finish_job(job, view, now)
            return
        if live:
            row = live[-1]
            age = (now - datetime.fromisoformat(row.sent_at)).total_seconds()
            if row.order_type == "limit" and row.state in (ACCEPTED, PARTIAL) and age >= self._policy.requote_secs:
                self._request_cancel(row, now)
            return
        if job.retry_after is not None and now < job.retry_after:
            return
        busy = sum(row.remaining for row in self._ledger.orders(trade_id=trade_id, live=True, kinds=("target",)))
        lots = min(wanted, view.position - busy)
        if lots <= 0:
            return        # ждём подтверждения снятия заявок целей
        card = self._card_for(view.plan.instrument_id)
        if card is None:
            return
        attempt = job.attempt
        if attempt > self._policy.attempts:
            row = LedgerOrder(order_key(f"{job.job_id}:{attempt}"), job.job_id, trade_id, "emergency",
                              card.uid, view.exit_side, "market", lots, job=job.job_id)
            if not job.alarmed:
                job.alarmed = True
                self._notify("emergency_market", row)
        else:
            base = job.base if attempt == 0 and job.base is not None else self._last_price(card.uid)
            if base is None:
                return
            price = exit_limit(card, base, view.exit_side, self._policy.offset_steps)
            row = LedgerOrder(order_key(f"{job.job_id}:{attempt}"), job.job_id, trade_id, job.kind, card.uid,
                              view.exit_side, "limit", lots, price, job=job.job_id)
            if attempt > 0:
                self._notify("requoted", row, attempt=attempt)
        existing = self._ledger.get(row.key)
        row = existing if existing is not None else self._send(row, now)
        if row.state == REJECTED:
            job.retry_after = now + timedelta(seconds=self._policy.retry_secs)
            job.attempt += 1          # повтор — новым ключом
            if not job.rejected_reported:
                job.rejected_reported = True
                self._notify("exit_rejected", row, reason=row.reason)
            return
        job.attempt += 1

    def _finish_job(self, job: ExitJob, view: TradeView, now: datetime) -> None:
        trade_id = job.trade_id
        self._jobs.pop(trade_id, None)
        if job.kind == "exit" and job.executed == 0 and view.position <= 0 and job.command_id:
            self._events.append(ExecutionEvent(
                execution_id=f"{job.command_id}:reject", order_id=job.command_id, command_id=job.command_id,
                trade_id=trade_id, status=ExecutionStatus.REJECT, filled_quantity=0, price=None, fee=Decimal(0),
                timestamp=now, reason="trade-not-open", fee_source=FeeSource.UNKNOWN))
        queued = self._queued.get(trade_id) or []
        if queued:
            self._jobs[trade_id] = queued.pop(0)
            self._pump(trade_id, now)

    def _ensure_targets(self, view: TradeView, now: datetime) -> None:
        """Лимитные заявки целей на остаток после входа; снятые брокером в конце дня ставятся заново."""
        trade_id = view.plan.trade_id
        if view.position <= 0 or trade_id in self._jobs or view.average is None:
            return
        card = self._card_for(view.plan.instrument_id)
        if card is None:
            return
        rows = self._ledger.orders(trade_id=trade_id, kinds=("target",))
        budget = view.position
        ordered = sorted(view.plan.targets, key=lambda t: t.price, reverse=view.plan.side == "SELL")
        for target in ordered:
            wanted = min(budget, max(0, JournalBroker._target_remaining(view, target.target_id)))
            budget -= wanted
            price = target_limit(card, target.price, view.exit_side)
            live = [row for row in rows if row.target_id == target.target_id and row.live]
            if live:
                row = live[-1]
                if row.state != CANCELLING and (row.remaining != wanted or row.price != price):
                    self._request_cancel(row, now)
                continue
            if wanted <= 0:
                continue
            retry = self._target_retry.get(f"{trade_id}:{target.target_id}")
            if retry is not None and now < retry:
                continue
            number = sum(1 for row in rows if row.target_id == target.target_id)
            row = self._send(LedgerOrder(order_key(f"{trade_id}:tp:{target.target_id}:{number}"),
                                         f"{trade_id}:tp:{target.target_id}", trade_id, "target", card.uid,
                                         view.exit_side, "limit", wanted, price, target_id=target.target_id), now)
            if row.state == REJECTED:
                self._target_retry[f"{trade_id}:{target.target_id}"] = now + timedelta(seconds=self._policy.retry_secs)
                if retry is None:
                    self._notify("exit_rejected", row, reason=row.reason)

    def _last_price(self, uid: str) -> Decimal | None:
        try:
            price = self._api.last_price(uid)
        except OrderFailure:
            price = None
        return price or self._last_close.get(self.key_for(uid))

    # --- исполнения в TradeManager -------------------------------------------
    def drain_addressed_events(self) -> list[ExecutionEvent]:
        """Новые сделки биржи и итоги заявок — события для ``consume`` по порядку прихода."""
        with self._lock:
            now = self._clock()
            for execution in self._ledger.pending_executions():
                if self._journal_has(execution.execution_id):
                    self._ledger.mark_consumed(execution.execution_id)
                    continue
                if execution.execution_id in self._emitted:
                    continue
                row = self._ledger.get(execution.key)
                event = self._execution_event(row, execution)
                self._emitted.add(execution.execution_id)
                if event is not None:
                    self._events.append(event)
                    self.fills_since_capital = True
            for row in self._ledger.unreported():
                if row.kind not in ENTRY_KINDS:
                    self._ledger.update(row.key, reported=1)
                    continue
                event = self._entry_outcome(row, now)
                if self._journal_has(event.execution_id):
                    self._ledger.update(row.key, reported=1)
                elif event.execution_id not in self._emitted:
                    self._emitted.add(event.execution_id)
                    self._events.append(event)
            self._finish_cancels(now)
            events, self._events = self._events, []
            return events

    def drain_events(self) -> list:
        return []

    def _entry_outcome(self, row: LedgerOrder, now: datetime) -> ExecutionEvent:
        view = self._views.get(row.trade_id)
        if row.state == REJECTED:
            status, reason = ExecutionStatus.REJECT, row.reason or "broker-rejected"
        else:
            status = ExecutionStatus.CANCEL
            reason = row.reason if row.reason == "entry-undelivered" else (
                view.cancel_reason if view is not None and view.cancel_reason else "broker-cancelled")
        event = self._outcome_ids(row.command_id, row.trade_id, now, status, reason)
        return replace(event, order_id=row.broker_order_id or row.command_id)

    def _finish_cancels(self, now: datetime) -> None:
        """Снятие входа завершено, когда все входы сделки завершены: итог самой команды отмены."""
        for view in self._views.values():
            if view.cancel_command is None:
                continue
            if self._ledger.orders(trade_id=view.plan.trade_id, live=True, kinds=ENTRY_KINDS):
                continue
            event = self._outcome_ids(view.cancel_command, view.plan.trade_id, now, ExecutionStatus.CANCEL,
                                      view.cancel_reason or "cancel")
            if not self._journal_has(event.execution_id) and event.execution_id not in self._emitted:
                self._emitted.add(event.execution_id)
                self._events.append(event)
            view.cancel_command = None

    def _execution_event(self, row: LedgerOrder, execution: LedgerExecution) -> ExecutionEvent | None:
        view = self._views.get(row.trade_id)
        if view is None:
            log.warning("Сделка биржи по неизвестной сделке журнала: %s", row.trade_id)
            return None
        plan = view.plan
        common = dict(execution_id=execution.execution_id, order_id=row.broker_order_id or row.key,
                      trade_id=row.trade_id, filled_quantity=execution.lots, price=execution.price, fee=None,
                      timestamp=execution.at, fee_source=FeeSource.BROKER)
        if row.kind in ENTRY_KINDS:
            done = self._done_through(row.trade_id, ENTRY_KINDS, lambda other: other.command_id == row.command_id,
                                      execution)
            self._apply_entry_fill(view, execution)
            status = ExecutionStatus.FILL if done >= row.lots else ExecutionStatus.PARTIAL
            return ExecutionEvent(command_id=row.command_id, status=status, reason="limit-fill",
                                  reference_price=row.price, reference_kind="signal", order_side=plan.side,
                                  slippage_source="broker", **common)
        view.position = max(0, view.position - execution.lots)
        if row.kind == "target":
            view.target_filled[row.target_id] = view.target_filled.get(row.target_id, 0) + execution.lots
            target = next((t for t in plan.targets if t.target_id == row.target_id), None)
            command = f"{row.trade_id}:pv:tp:{row.target_id}:{execution.execution_id.removeprefix('tinvest:')}"
            return ExecutionEvent(command_id=command, status=ExecutionStatus.FILL, reason=f"tp:{row.target_id}",
                                  reference_price=None if target is None else target.price, reference_kind="target",
                                  order_side=view.exit_side, slippage_source="broker", **common)
        job = self._jobs.get(row.trade_id)
        if job is not None and job.job_id == row.job:
            job.executed += execution.lots
        if ":pv:stop:" in row.job:
            command = f"{row.job}:{execution.execution_id.removeprefix('tinvest:')}"
            return ExecutionEvent(command_id=command, status=ExecutionStatus.FILL, reason="protective",
                                  reference_price=view.confirmed_stop, reference_kind="stop",
                                  order_side=view.exit_side, slippage_source="broker", **common)
        done = self._done_through(row.trade_id, EXIT_KINDS, lambda other: other.job == row.job, execution)
        total = job.quantity if job is not None and job.job_id == row.job and job.quantity else None
        status = ExecutionStatus.FILL if total is None or done >= total else ExecutionStatus.PARTIAL
        if total is None and view.position > 0:
            status = ExecutionStatus.PARTIAL
        return ExecutionEvent(command_id=row.job, status=status, reason=job.reason if job else "exit",
                              reference_price=job.reference if job else None, reference_kind="market-exit",
                              order_side=view.exit_side, slippage_source="broker", **common)

    def _done_through(self, trade_id: str, kinds, same, execution: LedgerExecution) -> int:
        """Лоты команды по её заявкам до этой сделки биржи включительно (порядок прихода)."""
        rows = [other for other in self._ledger.orders(trade_id=trade_id, kinds=kinds) if same(other)]
        items = sorted((e for other in rows for e in self._ledger.executions(other.key)), key=lambda e: e.at)
        total = 0
        for item in items:
            total += item.lots
            if item.execution_id == execution.execution_id:
                break
        return total

    def _apply_entry_fill(self, view: TradeView, execution: LedgerExecution) -> None:
        previous = view.position
        view.position += execution.lots
        view.entry_quantity += execution.lots
        view.average = execution.price if view.average is None or previous == 0 else (
            (view.average * previous + execution.price * execution.lots) / view.position)
        current = view.plan.algorithm_version == "economics-v2"
        rebased = rebase_on_average(view.original_plan if current else view.plan, view.average)
        if current:
            previous_targets = {t.target_id: t for t in view.plan.targets}
            rebased = replace(rebased, targets=tuple(previous_targets[t.target_id]
                              if view.target_filled.get(t.target_id, 0) else t for t in rebased.targets))
        view.plan = rebased
        view.confirmed_stop = (max(view.confirmed_stop, view.plan.stop_price) if view.plan.side == "BUY"
                               else min(view.confirmed_stop, view.plan.stop_price)) \
            if view.confirmed_stop is not None and current else view.plan.stop_price

    # --- сводка -------------------------------------------------------------
    def in_flight_instruments(self) -> set[str]:
        with self._lock:
            pending = {self._ledger.get(e.key).instrument_uid for e in self._ledger.pending_executions()
                       if not self._journal_has(e.execution_id)}
            return pending | {row.instrument_uid for row in self._ledger.orders(live=True)
                              if row.state in (SENDING, UNKNOWN, CANCELLING)}

    def live_orders(self) -> list[LedgerOrder]:
        return self._ledger.orders(live=True)

    def open_positions(self) -> dict[str, int]:
        """Знаковые лоты открытых сделок по UID брокера (сверщик)."""
        with self._lock:
            result: dict[str, int] = {}
            for view in self._views.values():
                if view.position > 0:
                    card = self._card_for(view.plan.instrument_id)
                    uid = card.uid if card is not None else view.plan.instrument_id
                    sign = 1 if view.plan.side == "BUY" else -1
                    result[uid] = result.get(uid, 0) + sign * view.position
            return result

    def busy(self) -> bool:
        """Есть ли открытые позиции или незавершённые заявки — контур ещё не может завершиться."""
        with self._lock:
            return any(view.position > 0 for view in self._views.values()) or bool(self._ledger.orders(live=True))

    # --- служебное ------------------------------------------------------------
    def _notify(self, kind: str, row: LedgerOrder, **fields) -> None:
        self._publish(Event.broker_order(
            kind, contour=self._contour, trade_id=row.trade_id, event_id=str(uuid.uuid4()),
            instrument=self._display(row.instrument_uid), bar_time=self._clock(), order=row.kind, order_type=row.order_type, side=row.side,
            lots=row.lots, price=row.price,
            broker_order=row.broker_order_id[-4:] if row.broker_order_id else None, **fields))

    @staticmethod
    def _outcome(action: TradeAction, now: datetime, status: ExecutionStatus, reason: str) -> ExecutionEvent:
        return TInvestBroker._outcome_ids(action.command_id, action.trade_id, now, status, reason)

    @staticmethod
    def _outcome_ids(command_id: str, trade_id: str, now: datetime, status: ExecutionStatus,
                     reason: str) -> ExecutionEvent:
        execution_id = f"{command_id}:{status.value}"
        if status is ExecutionStatus.CANCEL:
            execution_id = f"cancel:{trade_id}:{(reason or 'cancel').replace(':', '-')}:{command_id}"
        return ExecutionEvent(execution_id=execution_id, order_id=command_id, command_id=command_id,
                              trade_id=trade_id, status=status, filled_quantity=0, price=None, fee=Decimal("0"),
                              timestamp=now, reason=reason, fee_source=FeeSource.UNKNOWN)
