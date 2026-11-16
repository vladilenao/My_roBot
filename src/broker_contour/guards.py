"""Предохранители контура на брокере (add-account-guards): дневной убыток и отказы брокера подряд.

Состояние — в ``broker.sqlite3`` (таблицы ``broker_guards`` и ``broker_days``) и переживает рестарт.
Сработавший предохранитель запрещает новые входы и доборы по всему контуру через
``TradeManager.guard_entries``; выходы, стоп и цели у брокера работают.

- **Дневной убыток** (design.md, решение 2): закрытый результат исполнений журнала с начала торгового
  дня (19:00 МСК) плюс открытый результат по последней цене; лимит — процент капитала на начало дня.
  Снимается сам на первом опросе следующего дня.
- **Отказы подряд** (решение 3): отказы брокера на вход, добор и стоп-заявку; принятая заявка обнуляет
  счётчик. Снимается только поколением сброса из ядра.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from src.logging_setup import get_logger
from src.trade_management.manager import GUARD_BROKER_REJECTS, GUARD_DAILY_LOSS

log = get_logger(__name__)

MSK = ZoneInfo("Europe/Moscow")
DAY_START_HOUR = 19
DAILY_LOSS, BROKER_REJECTS = "daily_loss", "broker_rejects"
KINDS = (DAILY_LOSS, BROKER_REJECTS)
MANAGER_REASON = {DAILY_LOSS: GUARD_DAILY_LOSS, BROKER_REJECTS: GUARD_BROKER_REJECTS}
CLEAR, TRIPPED = "clear", "tripped"
EVALUATE_EVERY = timedelta(seconds=10)
REASONS_KEPT = 3
_INCREASE = {"OPEN", "ADD"}
_ZERO = Decimal(0)


def trading_day_start(now: datetime) -> datetime:
    """Начало торгового дня — последние 19:00 МСК не позже ``now``, в UTC."""
    local = now.astimezone(MSK)
    start = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


@dataclass(frozen=True)
class GuardState:
    kind: str
    state: str = CLEAR
    since: str | None = None
    until: str | None = None
    detail: dict = field(default_factory=dict)
    reset_generation: int = 0

    @property
    def tripped(self) -> bool:
        return self.state == TRIPPED

    def payload(self) -> dict:
        return {"kind": self.kind, "since": self.since, "until": self.until, "detail": dict(self.detail)}


class GuardStore:
    """Таблицы предохранителей в ``broker.sqlite3``: то же соединение и замок, что у журнала заявок."""

    def __init__(self, ledger) -> None:
        self._conn, self._lock = ledger.connection()
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS broker_guards (
                    kind TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    since TEXT,
                    until TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    reset_generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS broker_days (
                    day_start TEXT PRIMARY KEY,
                    start_capital TEXT NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL
                );
            """)

    def get(self, kind: str) -> GuardState:
        with self._lock:
            row = self._conn.execute("SELECT state, since, until, detail, reset_generation FROM broker_guards "
                                     "WHERE kind=?", (kind,)).fetchone()
        if row is None:
            return GuardState(kind)
        return GuardState(kind, row[0], row[1], row[2], json.loads(row[3] or "{}"), int(row[4]))

    def save(self, state: GuardState) -> GuardState:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO broker_guards(kind, state, since, until, detail, reset_generation) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET state=excluded.state, since=excluded.since, until=excluded.until, "
                "detail=excluded.detail, reset_generation=excluded.reset_generation",
                (state.kind, state.state, state.since, state.until, json.dumps(state.detail, ensure_ascii=False),
                 state.reset_generation))
        return state

    def day(self, day_start: datetime) -> tuple[Decimal, bool] | None:
        with self._lock:
            row = self._conn.execute("SELECT start_capital, partial FROM broker_days WHERE day_start=?",
                                     (day_start.isoformat(),)).fetchone()
        return None if row is None else (Decimal(row[0]), bool(row[1]))

    def record_day(self, day_start: datetime, capital: Decimal, partial: bool, now: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO broker_days(day_start, start_capital, partial, recorded_at) "
                               "VALUES(?,?,?,?)", (day_start.isoformat(), format(capital, "f"), int(partial),
                                                   now.isoformat()))


@dataclass(frozen=True)
class DayResult:
    closed: Decimal
    open: Decimal
    missing_prices: tuple[str, ...] = ()

    @property
    def loss(self) -> Decimal:
        total = self.closed + self.open
        return -total if total < 0 else _ZERO


def day_result(conn: sqlite3.Connection, day_start: datetime, prices: Mapping[str, Decimal]) -> DayResult:
    """Закрытый результат исполнений журнала с ``day_start`` и открытый результат по последней цене.

    Выход даёт ``(цена − средняя входа) × лоты × стоимость шага / шаг`` со знаком стороны, средняя —
    по исполнениям входа сделки до выхода; из суммы вычитаются комиссии всех исполнений дня.
    """
    rows = conn.execute(
        "SELECT t.trade_id, t.instrument_id, t.side, t.price_step, t.step_cost, o.action_type, f.quantity, f.price, "
        "f.fee, f.executed_at FROM fills f JOIN orders o ON o.order_id = f.order_id JOIN trades t ON t.trade_id = f.trade_id "
        "ORDER BY f.trade_id, f.executed_at, f.rowid").fetchall()
    closed, opened, missing = _ZERO, _ZERO, []
    trades: dict[str, dict] = {}
    for trade_id, instrument, side, step, cost, action, qty, price, fee, at in rows:
        sign = Decimal(1) if side == "BUY" else Decimal(-1)
        step_d, cost_d = Decimal(str(step or 0)), Decimal(str(cost or 0))
        mult = cost_d / step_d if step_d > 0 and cost_d > 0 else Decimal(1)
        trade = trades.setdefault(trade_id, {"instrument": instrument, "sign": sign, "mult": mult, "qty": 0,
                                             "avg": _ZERO})
        qty, price = int(qty), Decimal(str(price))
        today = _moment(at) >= day_start
        if str(action or "").upper().split(":", 1)[0] in _INCREASE:
            total = trade["qty"] + qty
            trade["avg"] = (trade["avg"] * trade["qty"] + price * qty) / total if total else price
            trade["qty"] = total
        else:
            take = min(qty, trade["qty"])
            if today:
                closed += (price - trade["avg"]) * take * sign * mult
            trade["qty"] -= take
        if today:
            closed -= Decimal(str(fee or 0))
    for trade in trades.values():
        if trade["qty"] <= 0:
            continue
        last = prices.get(str(trade["instrument"]))
        if last is None:
            missing.append(str(trade["instrument"]))
            continue
        opened += (Decimal(str(last)) - trade["avg"]) * trade["qty"] * trade["sign"] * trade["mult"]
    return DayResult(closed, opened, tuple(sorted(set(missing))))


def _moment(value) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


class Guards:
    """Предохранители одного контура: расчёт, срабатывание, снятие, запрет входов, уведомления."""

    def __init__(self, store: GuardStore, *, max_daily_loss_pct: float, max_rejects_in_row: int,
                 clock: Callable[[], datetime], tripped: Callable[[GuardState], None] = lambda state: None,
                 cleared: Callable[[GuardState, str], None] = lambda state, why: None) -> None:
        self._store = store
        self._pct = Decimal(str(max_daily_loss_pct))
        self._max_rejects = max_rejects_in_row
        self._clock = clock
        self._tripped = tripped
        self._cleared = cleared
        self._manager = None
        self._evaluated: datetime | None = None

    # --- запрет входов -----------------------------------------------------------
    def attach(self, manager) -> None:
        """Менеджер сделок контура; сработавшие предохранители применяются сразу (до первого тика)."""
        self._manager = manager
        for kind in KINDS:
            manager.guard_entries(MANAGER_REASON[kind], self._store.get(kind).tripped)

    def _apply(self, state: GuardState) -> None:
        if self._manager is not None:
            self._manager.guard_entries(MANAGER_REASON[state.kind], state.tripped)

    def states(self) -> list[dict]:
        """Сработавшие предохранители для ``status.json`` и ``current.guards``."""
        return [state.payload() for kind in KINDS if (state := self._store.get(kind)).tripped]

    # --- дневной убыток ------------------------------------------------------------
    def evaluate_daily(self, capital: Decimal | None, journal: sqlite3.Connection | None,
                       prices: Mapping[str, Decimal], *, force: bool = False) -> None:
        now = self._clock()
        if not force and self._evaluated is not None and now - self._evaluated < EVALUATE_EVERY:
            return
        self._evaluated = now
        day_start = trading_day_start(now)
        state = self._store.get(DAILY_LOSS)
        if state.tripped and state.until is not None and now >= datetime.fromisoformat(state.until):
            state = self._store.save(replace(state, state=CLEAR, since=None, until=None, detail={}))
            self._apply(state)
            log.info("Предохранитель «дневной убыток» снят: новый торговый день")
            self._cleared(state, "new-day")
        day = self._store.day(day_start)
        if day is None and capital is not None:
            partial = self._evaluated_before(day_start)
            self._store.record_day(day_start, Decimal(str(capital)), partial, now)
            day = self._store.day(day_start)
        if self._pct <= 0 or day is None or journal is None or state.tripped:
            return
        start_capital, partial = day
        try:
            result = day_result(journal, day_start, prices)
        except sqlite3.Error as exc:
            log.warning("Убыток дня не посчитан: журнал сделок не прочитан (%s)", type(exc).__name__)
            return
        limit = start_capital * self._pct / 100
        if result.loss < limit or limit <= 0:
            return
        detail = {"loss": _money(result.loss), "limit": _money(limit), "start_capital": _money(start_capital),
                  "partial_day": partial, "missing_prices": list(result.missing_prices)}
        state = self._store.save(GuardState(DAILY_LOSS, TRIPPED, now.isoformat(),
                                            (day_start + timedelta(days=1)).isoformat(), detail,
                                            state.reset_generation))
        self._apply(state)
        log.warning("Предохранитель «дневной убыток»: убыток %s ₽ при лимите %s ₽ — новые входы запрещены до %s",
                    detail["loss"], detail["limit"], state.until)
        self._tripped(state)

    def _evaluated_before(self, day_start: datetime) -> bool:
        """Контур поднялся посреди дня: снимок капитала — не на начало дня."""
        return self._clock() - day_start > EVALUATE_EVERY * 6

    # --- отказы подряд -------------------------------------------------------------
    def order_outcome(self, accepted: bool, reason: str = "") -> None:
        """Итог заявки входа или стоп-заявки у брокера: принята — счётчик в ноль, отказ — плюс один."""
        state = self._store.get(BROKER_REJECTS)
        if state.tripped:
            if not accepted:
                reasons = (list(state.detail.get("reasons", [])) + [reason])[-REASONS_KEPT:]
                self._store.save(replace(state, detail={**state.detail, "count": int(state.detail.get("count", 0)) + 1,
                                                        "reasons": reasons}))
            return
        if accepted:
            if state.detail.get("count"):
                self._store.save(replace(state, detail={}))
            return
        count = int(state.detail.get("count", 0)) + 1
        reasons = (list(state.detail.get("reasons", [])) + [reason])[-REASONS_KEPT:]
        if count < self._max_rejects:
            self._store.save(replace(state, detail={"count": count, "reasons": reasons}))
            return
        state = self._store.save(GuardState(BROKER_REJECTS, TRIPPED, self._clock().isoformat(), None,
                                            {"count": count, "reasons": reasons}, state.reset_generation))
        self._apply(state)
        log.warning("Предохранитель «отказы брокера подряд»: %d отказов (%s) — новые входы запрещены до сброса",
                    count, ", ".join(reasons))
        self._tripped(state)

    def apply_reset(self, generation: int | None) -> None:
        """Поколение сброса из желаемого состояния ядра: новее применённого — снять «отказы подряд»."""
        if generation is None:
            return
        state = self._store.get(BROKER_REJECTS)
        if generation <= state.reset_generation:
            return
        was = state.tripped
        state = self._store.save(GuardState(BROKER_REJECTS, CLEAR, None, None, {}, generation))
        self._apply(state)
        if was:
            log.info("Предохранитель «отказы брокера подряд» снят командой сброса")
            self._cleared(state, "command")
