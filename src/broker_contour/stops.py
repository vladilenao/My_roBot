"""Стоп у брокера (add-broker-protection): журнал стоп-заявок и хранитель стопа.

**Журнал** — таблица ``broker_stop_orders`` в ``broker.sqlite3`` рядом с журналом заявок
(design.md, решение 1). Строка пишется до постановки. Номер стоп-заявки хранится здесь;
стоп-заявку с потерянным ответом ищет адаптер брокера (``StopPort.poll_stops``: у Т-Инвест —
по инструменту, стороне, объёму и цене).

**Хранитель** (решения 2–6) — примесь к ``LiveBroker``: у каждой открытой сделки одна
стоп-заявка ``STOP_LOSS`` на остаток минус выходы контура в работе, по подтверждённому
стопу. Изменилось желаемое — «снять → дождаться → поставить», двух живых стоп-заявок одной
сделки не бывает. Срабатывание у брокера распознаётся по ``exchange_order_id`` в ответе
опроса; порождённая рыночная заявка становится строкой журнала заявок вида
``broker_stop`` и проводится защитным выходом сделки.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from src.broker_contour.orders import ACCEPTED, LedgerOrder, OrderLedger, order_key
from src.broker_contour.port import OrderFailure, OrderReport, StopQuery, StopReport
from src.broker_contour.prices import to_step
from src.events.event import Event
from src.logging_setup import get_logger

log = get_logger(__name__)

SENDING, ACTIVE, CANCELLING, UNKNOWN = "sending", "active", "cancelling", "unknown"
CANCELLED, EXECUTED, EXPIRED, REJECTED = "cancelled", "executed", "expired", "rejected"
# Ответ на постановку потерян, у брокера не найдена за STOP_LOST: вместо неё поставлена новая новым ключом, а эта ещё
# STOP_LOST_WINDOW ищется у брокера — найдётся, будет снята как лишняя (fix-stop-key-duplicate). Не «живая» для
# хранителя и сводки, но опрашивается.
LOST = "lost"
LIVE = frozenset({SENDING, ACTIVE, CANCELLING, UNKNOWN})
IN_FLIGHT = frozenset({SENDING, CANCELLING, UNKNOWN})
POLL_MIN_SECS = 2            # не больше 30 опросов GetStopOrders в минуту
POLL_EVERY_SECS = 60         # плановый опрос при живых стоп-заявках (как сверка)
STRANGER_SECS = 60           # незнакомый отчёт стрима ждёт своей стоп-заявки, потом он чужой
STOP_LOST = timedelta(seconds=15)          # сколько ждать стоп-заявку с потерянным ответом, прежде чем ставить новую
STOP_LOST_WINDOW = timedelta(minutes=10)   # сколько строка «потеряна» ищет свою стоп-заявку, чтобы снять лишнюю
CLOSED_RETRY = timedelta(minutes=1)


@dataclass(frozen=True)
class StopRow:
    key: str
    trade_id: str
    instrument_uid: str
    side: str                       # сторона выхода: SELL у длинной сделки
    lots: int
    stop_price: Decimal             # пункты
    number: int = 0                 # номер постановки у сделки
    state: str = SENDING
    stop_order_id: str = ""
    exchange_order_id: str = ""
    reason: str = ""
    own_cancel: int = 0             # снимал контур (а не брокер)
    created_at: str = ""
    sent_at: str = ""
    updated_at: str = ""

    @property
    def live(self) -> bool:
        return self.state in LIVE


_COLUMNS = tuple(f.name for f in fields(StopRow))


class StopLedger:
    """Строки стоп-заявок в ``broker.sqlite3``: то же соединение и замок, что у журнала заявок."""

    def __init__(self, ledger: OrderLedger) -> None:
        self._ledger = ledger
        self._conn, self._lock = ledger.connection()
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS broker_stop_orders (
                    key TEXT PRIMARY KEY,
                    trade_id TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    stop_price TEXT NOT NULL,
                    number INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    stop_order_id TEXT NOT NULL DEFAULT '',
                    exchange_order_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    own_cancel INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS broker_stop_orders_trade ON broker_stop_orders(trade_id, state);
            """)

    @staticmethod
    def _row(record) -> StopRow:
        data = dict(record)
        data["stop_price"] = Decimal(data["stop_price"])
        return StopRow(**data)

    def create(self, row: StopRow) -> StopRow:
        now = self._ledger.clock().isoformat()
        row = replace(row, created_at=row.created_at or now, sent_at=now, updated_at=now)
        values = tuple(format(row.stop_price, "f") if name == "stop_price" else getattr(row, name) for name in _COLUMNS)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT INTO broker_stop_orders({','.join(_COLUMNS)}) "
                               f"VALUES({','.join('?' * len(_COLUMNS))})", values)
        return row

    def update(self, key: str, **values) -> StopRow:
        values["updated_at"] = self._ledger.clock().isoformat()
        assignments = ", ".join(f"{name}=?" for name in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE broker_stop_orders SET {assignments} WHERE key=?",
                               (*(format(v, "f") if n == "stop_price" else v for n, v in values.items()), key))
        return self.get(key)

    def get(self, key: str) -> StopRow | None:
        with self._lock:
            record = self._conn.execute("SELECT * FROM broker_stop_orders WHERE key=?", (key,)).fetchone()
        return None if record is None else self._row(record)

    def rows(self, *, trade_id: str | None = None, live: bool | None = None,
             state: str | None = None) -> list[StopRow]:
        query, params = "SELECT * FROM broker_stop_orders WHERE 1=1", []
        if trade_id is not None:
            query += " AND trade_id=?"
            params.append(trade_id)
        if state is not None:
            query += " AND state=?"
            params.append(state)
        if live is not None:
            query += f" AND state {'' if live else 'NOT '}IN ({','.join('?' * len(LIVE))})"
            params.extend(sorted(LIVE))
        with self._lock:
            records = self._conn.execute(query + " ORDER BY created_at, rowid", params).fetchall()
        return [self._row(record) for record in records]

    def by_stop_id(self, stop_order_id: str) -> StopRow | None:
        with self._lock:
            record = self._conn.execute("SELECT * FROM broker_stop_orders WHERE stop_order_id=?",
                                        (stop_order_id,)).fetchone()
        return None if record is None else self._row(record)


def read_stop_rows(path) -> list[StopRow]:
    """Стоп-заявки контура только на чтение (API позиций в ядре); нет таблицы — пусто."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        records = conn.execute("SELECT * FROM broker_stop_orders ORDER BY created_at, rowid").fetchall()
        return [StopLedger._row(record) for record in records]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


@dataclass(frozen=True)
class WantedStop:
    price: Decimal
    lots: int


class StopKeeping:
    """Хранитель стопа у брокера — примесь к ``LiveBroker`` (поля и замок — его)."""

    _stops: StopLedger | None

    def _init_stops(self, stops: StopLedger | None) -> None:
        self._stops = stops
        self._strangers: dict[str, tuple[OrderReport, datetime]] = {}
        self._stop_need: dict[str, datetime] = {}          # сделка → с какого момента нет нужного стопа
        self._stop_retry: dict[str, datetime] = {}         # сделка → не ставить раньше (торги закрыты, отказ)
        self._stop_closed: set[str] = set()                # последняя постановка отклонена из-за закрытых торгов
        self._announced: dict[str, tuple[int, Decimal]] = {}
        self._last_stop_poll: datetime | None = None
        self._account_lots: dict[str, int] | None = None
        self._account_at: datetime | None = None
        self._fill_seen: dict[str, datetime] = {}          # UID → когда пришла последняя сделка биржи

    @property
    def stops_enabled(self) -> bool:
        return self._stops is not None

    def set_account_positions(self, holdings: dict[str, int] | None, at: datetime | None = None) -> None:
        """Знаковые лоты счёта по UID из снимка сверки: сумма стопов инструмента не больше позиции.

        ``at`` — момент, на который снят снимок. По инструменту, где после него пришла сделка биржи,
        снимок устарел и стоп не ограничивает: иначе новый вход остался бы без стопа до следующей сверки.
        """
        with self._lock:
            self._account_lots = None if holdings is None else dict(holdings)
            self._account_at = at or self._clock()

    def note_fill(self, uid: str) -> None:
        self._fill_seen[uid] = self._clock()

    # --- желаемый стоп ---------------------------------------------------------
    def _wanted_stops(self) -> dict[str, WantedStop]:
        result: dict[str, WantedStop] = {}
        by_uid: dict[str, list] = {}
        for view in self._views.values():
            trade_id = view.plan.trade_id
            if view.position <= 0 or view.confirmed_stop is None or view.average is None:
                continue
            job = self._jobs.get(trade_id)
            if job is not None and job.kind == "stop":
                continue
            exiting = 0
            if job is not None:
                if job.quantity is None:
                    continue                    # полный выход: стоп снимается раньше выхода (решение 3)
                exiting = max(0, job.quantity - job.executed)
            lots = view.position - exiting
            card = self._card_for(view.plan.instrument_id)
            if lots <= 0 or card is None:
                continue
            rounding = ROUND_CEILING if view.exit_side == "SELL" else ROUND_FLOOR
            result[trade_id] = WantedStop(to_step(view.confirmed_stop, card.price_step, rounding), lots)
            by_uid.setdefault(card.uid, []).append(view)
        if self._account_lots is not None:
            for uid, views in by_uid.items():
                seen = self._fill_seen.get(uid)
                if seen is not None and self._account_at is not None and seen > self._account_at:
                    continue
                sign = 1 if views[0].plan.side == "BUY" else -1
                room = max(0, sign * self._account_lots.get(uid, 0))
                total = sum(result[v.plan.trade_id].lots for v in views)
                for view in sorted(views, key=lambda v: (v.opened_at is not None, v.opened_at), reverse=True):
                    if total <= room:
                        break
                    wanted = result[view.plan.trade_id]
                    cut = min(wanted.lots, total - room)
                    total -= cut
                    if wanted.lots - cut > 0:
                        result[view.plan.trade_id] = replace(wanted, lots=wanted.lots - cut)
                    else:
                        result.pop(view.plan.trade_id)
        return result

    def broker_stop_covers(self, trade_id: str) -> bool:
        """У сделки активная стоп-заявка на весь желаемый объём по желаемой цене: контур стоп не проверяет."""
        if self._stops is None:
            return False
        wanted = self._wanted_stops().get(trade_id)
        if wanted is None:
            return False
        return any(row.state == ACTIVE and row.lots == wanted.lots and row.stop_price == wanted.price
                   for row in self._stops.rows(trade_id=trade_id, live=True))

    def stop_in_sync(self, trade_id: str) -> bool:
        """Добор допускается, только когда стоп сделки уже пересоздан на её остаток."""
        view = self._views.get(trade_id)
        if self._stops is None or view is None or view.position <= 0:
            return True
        return self.broker_stop_covers(trade_id)

    def stop_lots_live(self, trade_id: str) -> int:
        """Лоты живых стоп-заявок сделки: заявка выхода контура вместе с ними не больше остатка."""
        if self._stops is None:
            return 0
        return sum(row.lots for row in self._stops.rows(trade_id=trade_id, live=True))

    # --- хранитель -------------------------------------------------------------
    def keep_stops(self, now: datetime) -> None:
        """Привести стоп-заявки к желаемым; опросить брокера, если есть что выяснить."""
        if self._stops is None:
            return
        self._poll_stops(now)
        wanted = self._wanted_stops()
        trades = set(wanted) | {row.trade_id for row in self._stops.rows(live=True)}
        for trade_id in sorted(trades):
            if self._unsettled(trade_id):
                continue
            self._keep_trade(trade_id, wanted.get(trade_id), now)

    def _keep_trade(self, trade_id: str, want: WantedStop | None, now: datetime) -> None:
        live = self._stops.rows(trade_id=trade_id, live=True)
        if want is None:
            self._stop_need.pop(trade_id, None)
        if any(row.state in IN_FLIGHT for row in live):
            self._check_deadline(trade_id, want, now)
            return                              # ждём разбора: постановка или снятие ещё не подтверждены
        active = [row for row in live if row.state == ACTIVE]
        if want is not None and len(active) == 1 and (active[0].lots, active[0].stop_price) == (want.lots, want.price):
            self._stop_need.pop(trade_id, None)
            self._stop_closed.discard(trade_id)
            return
        for row in active:
            if not self._cancel_stop(row, now, removing=want is None):
                self._check_deadline(trade_id, want, now)
                return
        if want is None:
            return
        self._stop_need.setdefault(trade_id, now)
        retry = self._stop_retry.get(trade_id)
        if retry is None or now >= retry:
            self._post_stop(trade_id, want, now)
        self._check_deadline(trade_id, want, now)

    def _check_deadline(self, trade_id: str, want: WantedStop | None, now: datetime) -> None:
        since = self._stop_need.get(trade_id)
        if want is None or since is None or trade_id in self._stop_closed:
            return
        if (now - since).total_seconds() < self._policy.stop_place_secs:
            return
        view = self._views.get(trade_id)
        if view is None or view.position <= 0:
            return
        log.warning("Стоп-заявка %s %s не стала активной за %d с: выход и тревога", trade_id,
                    self._display(view.plan.instrument_id), self._policy.stop_place_secs)
        last = (self._stops.rows(trade_id=trade_id) or [None])[-1]
        self._stop_need.pop(trade_id, None)
        self._start_stop(view, now, reason="stop-not-placed", base=None)
        self._notify_stop("stop_failed", trade_id, view.plan.instrument_id, view.exit_side, want.lots, want.price,
                          None, reason=(last.reason if last is not None and last.state == REJECTED else "no-answer"))

    def _post_stop(self, trade_id: str, want: WantedStop, now: datetime) -> None:
        view = self._views[trade_id]
        card = self._card_for(view.plan.instrument_id)
        previous = self._stops.rows(trade_id=trade_id)
        number = len(previous)
        reason = self._stop_reason(view, previous[-1] if previous else None, want)
        row = self._stops.create(StopRow(order_key(f"{trade_id}:bs:{number}"), trade_id, card.uid, view.exit_side,
                                         want.lots, want.price, number=number, reason=reason))
        try:
            stop_id = self._api.post_stop(card.uid, view.exit_side, want.lots, want.price, row.key)
        except OrderFailure as failure:
            self._stop_failed(row, failure, now)
            return
        self._stop_closed.discard(trade_id)
        self._order_outcome(True, "")
        self._activated(self._stops.update(row.key, state=ACTIVE, stop_order_id=stop_id))

    def _stop_failed(self, row: StopRow, failure: OrderFailure, now: datetime) -> None:
        if failure.kind in ("unknown", "duplicate"):
            log.warning("Стоп-заявка %s: ответ брокера потерян (%s) — ищем у брокера", row.trade_id, failure.code)
            self._stops.update(row.key, state=UNKNOWN, reason=row.reason)
            return
        self._stops.update(row.key, state=REJECTED, reason=failure.reason)
        self._stop_retry[row.trade_id] = now + CLOSED_RETRY
        if failure.kind == "market_closed":
            self._stop_closed.add(row.trade_id)
            log.info("Стоп-заявка %s: торги закрыты, повтор через минуту, стоп держит контур", row.trade_id)
        else:
            log.warning("Стоп-заявка %s отклонена: %s", row.trade_id, failure)
            self._order_outcome(False, failure.reason)

    def _cancel_stop(self, row: StopRow, now: datetime, *, removing: bool) -> bool:
        """``True`` — снятие подтверждено сразу, можно ставить новую."""
        try:
            self._api.cancel_stop(row.stop_order_id)
        except OrderFailure as failure:
            log.warning("Снятие стоп-заявки %s: %s — уточняем у брокера", row.trade_id, failure.kind)
            self._stops.update(row.key, state=CANCELLING, own_cancel=1)
            self._last_stop_poll = None            # исполнена или снята — скажет ближайший опрос
            return False
        self._stops.update(row.key, state=CANCELLED, own_cancel=1)
        job = self._jobs.get(row.trade_id)
        if removing and job is not None and job.kind == "exit":
            self._notify_stop("stop_removed", row.trade_id, row.instrument_uid, row.side, row.lots, row.stop_price,
                              row.stop_order_id)
        return True

    def _activated(self, row: StopRow) -> None:
        """Стоп-заявка активна: строка карточки, если объём или цена изменились."""
        self._stop_need.pop(row.trade_id, None)
        mark = (row.lots, row.stop_price)
        if self._announced.get(row.trade_id) == mark:
            return
        replaced = row.trade_id in self._announced or row.number > 0
        self._announced[row.trade_id] = mark
        self._notify_stop("stop_placed", row.trade_id, row.instrument_uid, row.side, row.lots, row.stop_price,
                          row.stop_order_id, reason=row.reason, replaced=replaced)

    def _stop_reason(self, view, previous: StopRow | None, want: WantedStop) -> str:
        if previous is None:
            return "restart" if view.restored else "entry"
        if previous.stop_price != want.price:
            return "move"
        if want.lots > previous.lots:
            return "add"
        if want.lots < previous.lots:
            return "target" if any(view.target_filled.values()) and self._jobs.get(view.plan.trade_id) is None \
                else "reduce"
        return "replace"

    # --- опрос брокера и срабатывание ---------------------------------------------
    def note_stranger(self, report: OrderReport) -> None:
        """Отчёт стрима без строки журнала: возможно, заявка, порождённая нашей стоп-заявкой."""
        if self._stops is not None and report.broker_order_id and not report.temporary:
            self._strangers[report.broker_order_id] = (report, self._clock())

    def _poll_stops(self, now: datetime, *, force: bool = False) -> None:
        live = self._stops.rows(live=True)
        lost = self._stops.rows(state=LOST)
        if not live and not lost:
            self._strangers.clear()
            return
        needed = force or bool(self._strangers) or bool(lost) or any(row.state in IN_FLIGHT for row in live) \
            or self._last_stop_poll is None or (now - self._last_stop_poll).total_seconds() >= POLL_EVERY_SECS
        if not needed or (not force and self._last_stop_poll is not None
                          and (now - self._last_stop_poll).total_seconds() < POLL_MIN_SECS):
            return
        since = min(datetime.fromisoformat(row.created_at) for row in live + lost) - timedelta(minutes=1)
        bound = {row.stop_order_id for row in self._stops.rows() if row.stop_order_id}
        queries = [_query(row, search=row.state in (SENDING, UNKNOWN)) for row in live] + \
                  [_query(row, search=True, lost=True) for row in lost]
        try:
            matches = self._api.poll_stops(queries, bound, since, now + timedelta(minutes=1))
        except OrderFailure as failure:
            log.warning("Стоп-заявки не прочитаны: %s", failure.kind)
            return
        self._last_stop_poll = now
        for row, match in zip(live, matches[:len(live)]):
            report = match.report
            if match.searched:
                report = self._found_lost(row, match, now)
                if report is None:
                    continue
                row = self._stops.update(row.key, stop_order_id=report.stop_order_id)
            if report is not None:
                self._apply_stop_report(row, report, now)
        for row, match in zip(lost, matches[len(live):]):
            self._sweep_lost(row, match, now)
        for order_id, (_, seen) in list(self._strangers.items()):
            if (now - seen).total_seconds() >= STRANGER_SECS:
                self._strangers.pop(order_id, None)
                log.warning("Отчёт стрима по чужой заявке …%s: стоп-заявки с таким номером нет", order_id[-4:])

    def _found_lost(self, row: StopRow, match, now: datetime) -> StopReport | None:
        """Итог поиска стоп-заявки с потерянным ответом (решение 6): найдена одна — она.

        Не найдена — повтора тем же ключом нет (fix-stop-key-duplicate): песочница на него создаёт вторую
        стоп-заявку. ``STOP_LOST`` строка ждёт, затем становится «потеряна», и хранитель ставит новую новым ключом.
        """
        if match.report is not None:
            log.info("Стоп-заявка %s найдена у брокера по объёму и цене", row.trade_id)
            return match.report
        if match.candidates > 1:
            log.warning("Стоп-заявка %s: у брокера %d подходящих — оставлена «неизвестной»", row.trade_id,
                        match.candidates)
            return None
        if row.state == UNKNOWN:
            if now - datetime.fromisoformat(row.sent_at) < STOP_LOST:
                return None                         # брокер мог ещё не показать принятую стоп-заявку
            log.warning("Стоп-заявка %s: у брокера не найдена за %d с — ставим новую новым ключом, эту ищем %d мин",
                        row.trade_id, STOP_LOST.seconds, STOP_LOST_WINDOW.seconds // 60)
            self._stops.update(row.key, state=LOST)
            return None
        self._stops.update(row.key, state=CANCELLED, reason="undelivered")
        return None

    def _sweep_lost(self, row: StopRow, match, now: datetime) -> None:
        """Строка «потеряна»: её стоп-заявка показалась у брокера — снять как лишнюю; окно вышло — «не дошла»."""
        if match.report is None:
            if now - datetime.fromisoformat(row.sent_at) >= STOP_LOST_WINDOW:
                self._stops.update(row.key, state=CANCELLED, reason="undelivered")
            return
        stop_id = match.report.stop_order_id
        try:
            self._api.cancel_stop(stop_id)
        except OrderFailure as failure:
            log.warning("Лишняя стоп-заявка %s …%s: снятие %s — уточняем у брокера", row.trade_id, stop_id[-4:],
                        failure.kind)
            self._stops.update(row.key, state=CANCELLING, stop_order_id=stop_id, own_cancel=1, reason="duplicate")
            self._last_stop_poll = None
            return
        self._stops.update(row.key, state=CANCELLED, stop_order_id=stop_id, own_cancel=1, reason="duplicate")
        log.warning("Лишняя стоп-заявка %s …%s снята: ответ на её постановку был потерян", row.trade_id, stop_id[-4:])
        self._notify_stop("stop_duplicate_cancelled", row.trade_id, row.instrument_uid, row.side, row.lots,
                          row.stop_price, stop_id)

    def _apply_stop_report(self, row: StopRow, report: StopReport, now: datetime) -> None:
        if report.status == "active":
            if row.state in (SENDING, UNKNOWN):
                self._activated(self._stops.update(row.key, state=ACTIVE))
            elif row.state == CANCELLING:
                self._cancel_stop(row, now, removing=False)
        elif report.status == "cancelled":
            self._stops.update(row.key, state=CANCELLED)
        elif report.status == "expired":
            self._stops.update(row.key, state=EXPIRED)
        elif report.status == "executed":
            self._triggered(self._stops.update(row.key, state=EXECUTED,
                                               exchange_order_id=report.exchange_order_id), now)

    def _triggered(self, row: StopRow, now: datetime) -> None:
        """Стоп-заявка исполнена брокером: порождённая заявка — в журнал заявок, выход сделки — стоп-заданием."""
        self._announced.pop(row.trade_id, None)
        view = self._views.get(row.trade_id)
        job_id = f"{row.trade_id}:pv:stop:{row.stop_order_id}"
        log.info("Стоп-заявка %s %s исполнена брокером, заявка …%s", row.trade_id,
                 self._display(row.instrument_uid), row.exchange_order_id[-4:] if row.exchange_order_id else "?")
        if row.exchange_order_id:
            key = order_key(f"{job_id}:fill")
            order = self._ledger.get(key) or self._ledger.create(LedgerOrder(
                key, job_id, row.trade_id, "broker_stop", row.instrument_uid, row.side, "market", row.lots,
                state=ACCEPTED, broker_order_id=row.exchange_order_id, job=job_id))
            seen = self._strangers.pop(row.exchange_order_id, None)
            if seen is not None:
                self._apply_report(order, replace(seen[0], key=key), now)
            self._sync.add(key)                     # сделки — из GetOrderState по номеру биржи
        if view is not None:
            self._start_stop(view, now, job_id=job_id, base=None, reason="protective", attempt=1)
        self._notify_stop("stop_triggered", row.trade_id, row.instrument_uid, row.side, row.lots, row.stop_price,
                          row.stop_order_id, exchange_order=row.exchange_order_id[-4:] if row.exchange_order_id else None)

    def recover_stops(self, now: datetime) -> None:
        """До первого тика: «отправлена» без ответа — «неизвестно», затем опрос брокера и хранитель."""
        if self._stops is None:
            return
        for row in self._stops.rows(live=True):
            if row.state == SENDING:
                self._stops.update(row.key, state=UNKNOWN)
        self._poll_stops(now, force=True)

    # --- сводка -----------------------------------------------------------------
    def stop_in_flight_uids(self, now: datetime) -> set[str]:
        if self._stops is None:
            return set()
        uids = {row.instrument_uid for row in self._stops.rows(live=True) if row.state in IN_FLIGHT}
        uids |= {row.instrument_uid for row in self._stops.rows(state=LOST)}     # лишняя ещё может показаться
        return uids | {report.instrument_uid for report, seen in self._strangers.values()
                       if report.instrument_uid and (now - seen).total_seconds() < STRANGER_SECS}

    def active_stops(self) -> list[StopRow]:
        return [] if self._stops is None else [row for row in self._stops.rows(live=True) if row.state == ACTIVE]

    def live_stop_count(self) -> int:
        return 0 if self._stops is None else len(self._stops.rows(live=True))

    def _notify_stop(self, kind: str, trade_id: str, instrument: str, side: str, lots: int, price: Decimal,
                     stop_order_id: str | None, **fields) -> None:
        self._publish(Event.broker_order(
            kind, contour=self._contour, trade_id=trade_id, event_id=str(uuid.uuid4()),
            instrument=self._display(instrument), bar_time=self._clock(), order="broker_stop", order_type="stop",
            side=side, lots=lots, price=price, broker_order=stop_order_id[-4:] if stop_order_id else None, **fields))


def _query(row: StopRow, *, search: bool, lost: bool = False) -> StopQuery:
    return StopQuery(row.key, row.stop_order_id, row.instrument_uid, row.side, row.lots, row.stop_price,
                     datetime.fromisoformat(row.sent_at), search=search, lost=lost)
