"""Процесс контура на брокере (add-sandbox-contour, add-tinvest-orders).

Допуск до первого шага. При ``orders = false`` — шаг раз в ``step_secs``:
целевое состояние из ``core.sqlite3``, сверка по расписанию, ``status.json``;
остановка любого режима — выход на шаге её применения, счёт не трогается.

При ``orders = true`` после допуска поднимается торговая часть эмуляции с
``TInvestBroker`` (design.md решение 1): до первого тика разбираются
незавершённые заявки и непроведённые исполнения, проходит сверка; затем бот
работает на главном потоке, а слой брокера (исполнения, капитал, ГО, сверка,
``status.json``) — на границах тика и в опросе между тиками. Пульс ядра идёт
своим потоком (``CorePulse``) и через ``touch`` освежает ``alive_at``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.assembly.contour_control import STATUS_FILE, write_status
from src.assembly.core_state import read_desired
from src.broker_contour.account import AccountSnapshot, read_account
from src.broker_contour.orders import ACCEPTED, PARTIAL
from src.broker_contour.admission import AdmissionError, admit_account, check_settings
from src.broker_contour.notify import BrokerNotifier
from src.broker_contour.reconcile import JournalView, Reconciler
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.logging_setup import get_logger, register_secrets
from src.sandbox.actions import rub_balance
from src.sandbox.gateway import BrokerCallError
from src.sandbox.instruments import short_name_by_uid

log = get_logger(__name__)

STEP_SECS = 10
STOPPED, FAILED, RUNNING, STARTING = "stopped", "failed", "running", "starting"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BrokerContourSettings:
    contour_id: str
    broker: str
    directory: Path
    core_db: Path
    version: str
    token: str | None
    token_env: str
    bot_token: str | None
    bot_env: str
    channel_id: str | None
    channel_env: str
    reconcile_every_secs: float
    step_secs: float = STEP_SECS
    orders: bool = False
    exit_policy: object = None


class BrokerContour:
    """Процесс контура на брокере. ``gateway_factory(token)`` — контекстный менеджер шлюза брокера."""

    def __init__(self, settings: BrokerContourSettings, *, gateway_factory: Callable, bus, stop,
                 pid: int, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 trading_factory: Callable | None = None):
        self.settings = settings
        # ``trading_factory(trading, control, strategies)`` — торговая часть эмуляции (run.py);
        # нужна только при ``orders = true``.
        self._trading_factory = trading_factory
        self._bus = bus
        self._core_link = "ok"
        self._capital: Decimal | None = None
        settings.directory.mkdir(parents=True, exist_ok=True)
        self._gateway_factory = gateway_factory
        self._stop = stop
        self._pid = pid
        self._clock = clock
        self._lock = threading.Lock()
        self.notifier = BrokerNotifier(bus, contour=settings.contour_id)
        self._status = {"contour_id": settings.contour_id, "kind": settings.contour_id, "state": STARTING,
                        "applied_generation": None, "stop_mode": None, "version": settings.version, "pid": pid,
                        "members": [], "account": None, "capital": None, "positions": [], "orders": 0,
                        "stop_orders": 0, "frozen": [], "last_reconcile_at": None, "account_link": None,
                        "core_link": "ok", "reason": None, "exited_at": None, "exit_reason": None,
                        "orders_enabled": settings.orders, "trades": 0, "orders_in_flight": 0,
                        "stream_link": None}
        self._generation: int | None = None

    # --- status.json ------------------------------------------------------
    def _write(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)
            payload = {**self._status, "alive_at": _now(), "updated_at": _now()}
            try:
                write_status(self.settings.directory / STATUS_FILE, payload)
            except OSError as exc:
                log.warning("status.json контура не записан (%s)", type(exc).__name__)

    def touch(self, link: str) -> None:
        """Пульс ядра (поток ``CorePulse``): связь и свежий ``alive_at`` без ожидания шага."""
        self._core_link = link
        self._write(core_link=link)

    # --- жизнь процесса ---------------------------------------------------
    def run(self) -> int:
        settings = self.settings
        try:
            check_settings(token=settings.token, token_env=settings.token_env, bot_token=settings.bot_token,
                           bot_env=settings.bot_env, channel_id=settings.channel_id, channel_env=settings.channel_env)
        except AdmissionError as exc:
            return self._refuse(exc)
        store = BrokerStore(settings.directory / BROKER_DATABASE_FILE)
        try:
            with self._gateway_factory(settings.token) as gateway:
                try:
                    admitted = admit_account(gateway, store, broker=settings.broker)
                except AdmissionError as exc:
                    return self._refuse(exc)
                if settings.orders:
                    return self._serve_trading(gateway, store, admitted)
                return self._serve(gateway, store, admitted)
        except BrokerCallError as exc:
            return self._refuse(AdmissionError(f"Брокер недоступен: {exc}", alarm=True))

    def _refuse(self, exc: AdmissionError) -> int:
        log.error("Контур %s не стартует: %s", self.settings.contour_id, exc.reason)
        print(f"Контур {self.settings.contour_id} не стартует: {exc.reason}")
        if exc.alarm:
            self.notifier.account_tail = exc.account_tail
            self.notifier.alarm([], (), reason=f"Контур не стартует: {exc.reason}")
        self._write(state=FAILED, reason=exc.reason, exited_at=_now(), exit_reason=exc.reason)
        return 2

    def _serve(self, gateway, store: BrokerStore, admitted) -> int:
        settings = self.settings
        account = admitted.account
        register_secrets([account.account_id])
        self.notifier.account_tail = account.masked
        reconciler = Reconciler(store=store, notifier=self.notifier, account_tail=account.masked,
                                every_secs=settings.reconcile_every_secs)
        desired = read_desired(settings.core_db, settings.contour_id)
        self._apply(desired)
        self._account(admitted.snapshot)
        self.notifier.started(admitted.snapshot, bindings=len(self._status["members"]), generation=self._generation)
        log.info("Контур %s запущен: счёт %s, поколение %s", settings.contour_id, account.masked, self._generation)
        self._reconcile(reconciler, gateway, account, admitted.snapshot)
        self._write(state=RUNNING, account=account.masked, account_link="ok")
        while not self._stop.requested.wait(settings.step_secs):
            self.step(gateway, account, reconciler)
        reason = self._stop.reason or "цикл завершён"
        self.notifier.stopped(reason)
        self._write(state=STOPPED, exited_at=_now(), exit_reason=reason)
        log.info("Контур %s остановлен: %s", settings.contour_id, reason)
        return 0

    def step(self, gateway, account, reconciler: Reconciler) -> None:
        """Один шаг: целевое состояние, сверка по расписанию, ``status.json``."""
        self._apply(read_desired(self.settings.core_db, self.settings.contour_id))
        if self._stop.requested.is_set():
            return
        if reconciler.due(self._clock()):
            self._reconcile(reconciler, gateway, account)
        self._write()

    def _apply(self, desired) -> None:
        if desired is None:
            return
        if desired.generation != self._generation:
            self._generation = desired.generation
            members = [{"key": member.key, "name": member.key, "state": "watching" if member.active else "removed"}
                       for member in desired.members]
            self._write(applied_generation=desired.generation, members=members, stop_mode=desired.stop_mode)
            log.info("Контур %s: применено поколение %s", self.settings.contour_id, desired.generation)
        if not desired.running and desired.stop_mode:
            # С2: своих сделок нет — остановка любого режима завершает процесс; счёт не трогается.
            self._stop.request(f"остановка командой ({desired.stop_mode})")

    def _account(self, snapshot: AccountSnapshot) -> None:
        self._write(**snapshot.payload(), account_link="ok")

    def _reconcile(self, reconciler: Reconciler, gateway, account, snapshot: AccountSnapshot | None = None) -> None:
        now = self._clock()
        if snapshot is None:
            try:
                snapshot = read_account(gateway, account)
            except BrokerCallError as exc:
                log.warning("Сверка пропущена: счёт не прочитан (%s)", exc.code)
                report = reconciler.run(now, None)
                self._write(account_link="lost", last_reconcile_at=report["at"])
                return
            self._account(snapshot)
        report = reconciler.run(now, snapshot, JournalView())
        self._write(frozen=report["frozen"], last_reconcile_at=report["at"])

    # --- торговля привязками (add-tinvest-orders) -----------------------------
    def _serve_trading(self, gateway, store: BrokerStore, admitted) -> int:
        from src.assembly.contour import BrokerTrading
        from src.broker_contour.api import TInvestApi
        from src.broker_contour.broker import ExitPolicy, TInvestBroker
        from src.broker_contour.control import BrokerControl, assignment_index
        from src.broker_contour.orders import OrderLedger
        from src.broker_contour.stream import OrderStream
        from src.config import FUTURE_STRATEGIES, SHARE_STRATEGIES
        from src.trade_management.manager import _entry_ttl_seconds

        settings = self.settings
        account = admitted.account
        register_secrets([account.account_id])
        self.notifier.account_tail = account.masked
        api = TInvestApi(gateway, account.account_id)
        ledger = OrderLedger(settings.directory / BROKER_DATABASE_FILE)
        reconciler = Reconciler(store=store, notifier=self.notifier, account_tail=account.masked,
                                every_secs=settings.reconcile_every_secs)
        self._capital = admitted.snapshot.rub
        self._account(admitted.snapshot)
        holder: dict = {}

        def broker_factory(storage):
            def journal_has(execution_id: str) -> bool:
                return storage.connection.execute(
                    "SELECT 1 FROM fills WHERE execution_id=? UNION SELECT 1 FROM events WHERE event_id=?",
                    (execution_id, execution_id)).fetchone() is not None

            broker = TInvestBroker(api, ledger, contour=settings.contour_id,
                                   policy=settings.exit_policy or ExitPolicy(), publish=self._bus.publish,
                                   journal_has=journal_has, entry_ttl=_entry_ttl_seconds, clock=self._clock)
            holder["broker"] = broker
            return broker

        strategies: tuple[dict, dict] = ({}, {})
        control = BrokerControl(
            core_db=settings.core_db, contour_id=settings.contour_id,
            index=assignment_index(SHARE_STRATEGIES, FUTURE_STRATEGIES), strategies=strategies,
            request_stop=self._stop.request, status=self._write, poll=lambda: self._poll(holder, gateway, account,
                                                                                         reconciler),
            broker_busy=lambda: holder["broker"].busy() if "broker" in holder else False,
            core_lost=lambda: self._core_link == "lost")
        self._control = control
        trading = BrokerTrading(broker_factory=broker_factory, capital=lambda: self._capital,
                                initial_deposit=admitted.snapshot.rub, poll=control.apply)
        contour = self._trading_factory(trading, control, strategies)
        stream = None
        try:
            contour.build()
            broker, manager = holder["broker"], contour.runtime.trade_manager
            holder["manager"] = manager
            # До первого тика: «отправлена» без ответа и «неизвестно» — поиск по ключу; непроведённые
            # исполнения — в журнал; затем сверка (design.md решение 8).
            now = self._clock()
            broker.refresh_margins(now)
            broker.recover(now)
            self._consume(broker, manager)
            self._reconcile_trading(reconciler, gateway, account, broker, manager, admitted.snapshot)
            stream = OrderStream(api, broker, stop=self._stop.requested)
            stream.start()
            desired = read_desired(settings.core_db, settings.contour_id)
            self.notifier.started(admitted.snapshot, bindings=0 if desired is None else
                                  sum(1 for member in desired.members if member.active),
                                  generation=None if desired is None else desired.generation, orders_enabled=True)
            log.info("Контур %s запущен с заявками: счёт %s", settings.contour_id, account.masked)
            self._write(state=RUNNING, account=account.masked, account_link="ok")
            contour.run(contour.waiter())
        finally:
            if stream is not None:
                stream.close()
            contour.close()
        reason = self._stop.reason or "цикл завершён"
        positions = sum(1 for lots in holder["broker"].open_positions().values() if lots) if "broker" in holder else 0
        self.notifier.stopped(reason, open_positions=positions)
        self._write(state=STOPPED, exited_at=_now(), exit_reason=reason)
        ledger.close()
        log.info("Контур %s остановлен: %s", settings.contour_id, reason)
        return 0

    def _consume(self, broker, manager) -> int:
        events = broker.drain_addressed_events()
        for event in events:
            manager.consume(event)
        return len(events)

    def _poll(self, holder: dict, gateway, account, reconciler: Reconciler) -> None:
        """Опрос между тиками и граница тика: исполнения, капитал, ГО, сверка, доставка команд."""
        broker, manager = holder.get("broker"), holder.get("manager")
        if broker is None or manager is None:
            return
        now = self._clock()
        broker.service(now)
        self._consume(broker, manager)
        if broker.fills_since_capital:
            broker.fills_since_capital = False
            try:
                self._capital = rub_balance(gateway, account)
            except BrokerCallError as exc:
                log.warning("Капитал после исполнения не прочитан (%s)", exc.code)
        broker.refresh_margins(now)
        if reconciler.due(now):
            self._reconcile_trading(reconciler, gateway, account, broker, manager)
        try:
            manager.dispatch(now)
        except Exception as exc:  # noqa: BLE001 — доставка повторится на следующем опросе
            log.warning("Сбой доставки команд брокеру (%s)", type(exc).__name__)
        self._consume(broker, manager)
        positions, _ = manager.contour_remainder()
        self._write(trades=positions, orders_in_flight=len(broker.live_orders()), stream_link=broker.stream_link)

    def _reconcile_trading(self, reconciler: Reconciler, gateway, account, broker, manager,
                           snapshot: AccountSnapshot | None = None) -> None:
        """Сверка со своим журналом: позиции сделок, заявки в работе; «в пути» не сравнивается."""
        now = self._clock()
        self._consume(broker, manager)
        if snapshot is None:
            try:
                snapshot = read_account(gateway, account)
            except BrokerCallError as exc:
                log.warning("Сверка пропущена: счёт не прочитан (%s)", exc.code)
                report = reconciler.run(now, None)
                self._capital = None
                self._write(account_link="lost", last_reconcile_at=report["at"])
                return
        self._capital = snapshot.rub
        self._account(snapshot)
        names = snapshot.names

        def name(uid: str) -> str:
            try:
                return short_name_by_uid(gateway, uid, names)
            except BrokerCallError:
                return uid

        journal = JournalView(
            holdings=tuple((name(uid), lots) for uid, lots in broker.open_positions().items() if lots),
            orders=tuple(name(row.instrument_uid) for row in broker.live_orders()
                         if row.broker_order_id and row.state in (ACCEPTED, PARTIAL)))
        in_flight = {name(uid) for uid in broker.in_flight_instruments()}
        report = reconciler.run(now, snapshot, journal, in_flight=in_flight)
        by_name = {short: uid for uid, short in names.items()}
        manager.freeze_instruments({broker.key_for(by_name.get(item, item)) for item in report["frozen"]})
        self._write(frozen=report["frozen"], last_reconcile_at=report["at"])
