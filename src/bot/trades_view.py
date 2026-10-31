"""Позиции, сделки и итоги контура по журналу сделок (add-contour-trades-api): только чтение.

Ядро открывает ``trades.sqlite3`` контура в режиме только чтения, одной
транзакцией на ответ, — так же, как сверка читает ``broker.sqlite3``. Ответ
есть и у остановленного контура; журнала нет — пустой ответ, а не ошибка.

Сделка — сделка журнала: вход, доборы, частичные выходы, закрытие. Карточку
закрытой сделки собирает ``CsvExporter.trade_card``: исполнения, причины,
сценарий выхода и R те же, что в ``trade_summary.csv``. Биржевой тикер наружу
не выходит: инструмент — короткое имя из ``instrument_names`` журнала, сделка —
непрозрачный ``id`` вместо ``trade_id`` (в нём тикер).

Цена открытой позиции — закрытие последней завершённой ``1m`` из рыночной
базы ядра (решение владельца): одна и та же у эмуляции и песочницы и та же,
что на графике.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from src.bot import control_views as views
from src.data.market_store import SOURCE_TBANK
from src.trade_journal.export import CsvExporter
from src.trade_journal.reasons import reason_label

log = logging.getLogger(__name__)

PARTS = ("positions", "trades", "performance", "account")
LIMIT_DEFAULT = 200
LIMIT_MAX = 1000
CURVE_MAX_POINTS = 500
PERIODS = ("day", "week", "month", "all")
CURRENCY = "RUB"
# Площадка живёт по МСК без перехода на летнее время; торговый день открывает вечерняя сессия в 19:00.
_MSK = timezone(timedelta(hours=3))
_DAY_START_HOUR = 19
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_ENTRY_ACTIONS = ("OPEN", "ADD")
_PHASES = {"OPEN": "open", "BUILDING": "building", "REDUCING": "reducing"}

# Вид выхода (спецификация «Вид выхода»): сначала по типу заявки, у закрытия — по коду причины.
_KIND_BY_ACTION = {"TARGET": "target", "STOP": "stop", "REDUCE": "partial"}
_KIND_BY_CODE = {
    **dict.fromkeys(("raw-opposite-signal", "opposite-signal-management", "ma40-opposite-close"), "signal"),
    "intraday-session-end": "session_end",
    **dict.fromkeys(("contract-expiring", "contract-rollover"), "expiry"),
    **dict.fromkeys(("contour-stopping", "contour-stop", "instrument-removing", "instrument-remove",
                     "binding-removing", "binding-remove", "core-lost", "instrument-frozen"), "contour"),
}

_TRADE_ROWS = """
SELECT t.trade_id, t.assignment_id, t.instrument_id, t.side, t.phase, t.plan_json, t.price_step, t.step_cost,
       t.created_at, p.quantity, p.average_price, p.realized_pnl, p.fees, p.updated_at
FROM trades t JOIN positions p ON p.trade_id = t.trade_id
"""
_FILL_TIMES = """
SELECT f.trade_id,
       MIN(CASE WHEN {entry} THEN f.executed_at END) AS opened_at,
       MAX(CASE WHEN NOT ({entry}) THEN f.executed_at END) AS closed_at,
       GROUP_CONCAT(DISTINCT f.fee_source) AS fee_sources
FROM fills f JOIN orders o ON o.order_id = f.order_id
GROUP BY f.trade_id
""".format(entry="(o.action_type LIKE 'OPEN%' OR o.action_type LIKE 'ADD%')")


class TradesError(Exception):
    """Отказ ресурса торговли: код HTTP, код ошибки, причина."""

    def __init__(self, status: HTTPStatus, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def exit_kind(action_type: object, code: object) -> str:
    """Вид выхода для короткой метки UI; новый код причины — ``other``, а не ошибка."""
    action = str(action_type or "").upper().split(":", 1)[0]
    return _KIND_BY_ACTION.get(action) or _KIND_BY_CODE.get(str(code or ""), "other")


def trade_ref(trade_id: str) -> str:
    """Непрозрачный идентификатор сделки: ``trade_id`` журнала содержит биржевой тикер."""
    return "t_" + hashlib.sha256(trade_id.encode("utf-8")).hexdigest()[:16]


class ContourTrades:
    """Ресурсы торговли одного контура: ``…/positions``, ``…/trades``, ``…/performance``, ``…/account``."""

    def __init__(self, *, base: str, journal: Callable[[], Path | None], catalog: dict[str, dict],
                 initial_deposit: object | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.base = base
        self._journal = journal
        self._catalog = catalog
        # Депозит виртуального счёта; None — у контура нет ресурса счёта (песочница).
        self._initial = None if initial_deposit is None else Decimal(str(initial_deposit))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def parts(self) -> tuple[str, ...]:
        return PARTS if self._initial is not None else PARTS[:3]

    def links(self) -> dict[str, str]:
        """Ссылки состояния контура на ресурсы торговли."""
        return {part: f"{self.base}/{part}" for part in self.parts}

    # --- ресурсы ----------------------------------------------------------
    def positions(self, query: dict[str, list[str]], store=None) -> dict:
        _known(query, ("instrument",))
        instrument = _instrument_filter(query)
        with self._snapshot() as journal:
            items = [] if journal is None else journal.positions(_prices(store))
        items = [item for item in items if _matches(item, instrument)]
        unrealized, complete = _unrealized_total(items)
        return {"currency": CURRENCY, "as_of": _iso(self._clock()), "items": items,
                "unrealized": _money(unrealized), "unrealized_complete": complete,
                "_links": views.links(self=self._href("positions", query))}

    def trades(self, query: dict[str, list[str]]) -> tuple[dict, str]:
        """Страница закрытых сделок и ``ETag`` — номер последнего события журнала."""
        _known(query, ("from", "to", "limit", "instrument"))
        lower, upper = _window(query, self._clock())
        limit = _limit(query)
        instrument = _instrument_filter(query)
        with self._snapshot() as journal:
            if journal is None:
                return self._page([], False, lower, upper, limit, instrument), '"0"'
            closed = [row for row in journal.closed()
                      if (lower is None or row["closed_at"] >= lower) and row["closed_at"] < upper
                      and _matches(row, instrument)]
            closed.sort(key=lambda row: (row["closed_at"], row["trade_id"]), reverse=True)
            page = closed[:limit]
            if page:
                edge = page[-1]["closed_at"]
                page += [row for row in closed[limit:] if row["closed_at"] == edge]
            earlier = len(closed) > len(page)
            items = [journal.closed_card(row) for row in page]
            etag = f'"{journal.revision()}"'
        return self._page(items, earlier, lower, upper, limit, instrument, page[-1]["closed_at"] if page else None), etag

    def performance(self, query: dict[str, list[str]], store=None) -> dict:
        _known(query, ("period", "from", "to"))
        now = self._clock()
        name, lower, upper = _period(query, now)
        with self._snapshot() as journal:
            closed = [] if journal is None else journal.closed()
            positions = [] if journal is None else journal.positions(_prices(store))
        trades = [row for row in closed if (lower is None or row["closed_at"] >= lower) and row["closed_at"] < upper]
        trades.sort(key=lambda row: (row["closed_at"], row["trade_id"]))
        nets = [row["net"] for row in trades]
        winners = [net for net in nets if net > 0]
        losers = [net for net in nets if net < 0]
        start = lower if lower is not None else (min(row["opened_at"] for row in trades) if trades else upper)
        full = _curve(start, trades)
        drawdown, bottom = _drawdown(full)
        open_realized = sum((Decimal(item["realized"]) for item in positions), _ZERO)
        open_unrealized, complete = _unrealized_total(positions)
        trades_query = {"from": [_iso(lower)]} if lower is not None else {}
        if name == "custom":
            trades_query["to"] = [_iso(upper)]
        return {
            "currency": CURRENCY,
            "period": {"name": name, "from": _iso(lower), "to": _iso(upper)},
            "realized": {"net": _money(sum(nets, _ZERO)),
                         "gross": _money(sum((row["gross"] for row in trades), _ZERO)),
                         "fees": _money(sum((row["fees"] for row in trades), _ZERO)),
                         "fees_source": _fees_source(source for row in trades for source in row["fee_sources"])
                         if trades else None},
            "trades": {"count": len(trades), "winners": len(winners), "losers": len(losers)},
            "average_net": _money(sum(nets, _ZERO) / len(nets)) if nets else None,
            "profit_factor": _money(sum(winners, _ZERO) / -sum(losers, _ZERO)) if losers else None,
            "max_drawdown": {"amount": _money(drawdown), "at": _iso(bottom)},
            "open": {"count": len(positions), "realized": _money(open_realized),
                     "unrealized": _money(open_unrealized), "unrealized_complete": complete},
            "curve": [{"at": _iso(at), "net": _money(net)} for at, net in _thin(full)],
            "curve_now": {"at": _iso(now), "net": _money(full[-1][1] + open_realized + open_unrealized)},
            "_links": views.links(self=self._href("performance", query), trades=self._href("trades", trades_query)),
        }

    def account(self) -> dict:
        """Виртуальный счёт эмуляции; ``equity`` журнала без переоценки позиций — не отдаётся."""
        with self._snapshot() as journal:
            row = None if journal is None else journal.account()
        balance = realized = fees = None
        if row is not None:
            balance, realized, fees = (_decimal(row[key]) for key in ("balance", "net_realized_pnl", "fees"))
        return {"currency": CURRENCY, "initial": _money(self._initial),
                "balance": _money(self._initial if balance is None else balance),
                "realized_net": _money(realized or _ZERO), "fees": _money(fees or _ZERO),
                "updated_at": None if row is None else _iso(_utc(row["updated_at"])),
                "_links": views.links(self=f"{self.base}/account", performance=f"{self.base}/performance")}

    # --- журнал -----------------------------------------------------------
    def _snapshot(self) -> _Snapshot:
        return _Snapshot(self._journal(), self._binding)

    def _binding(self, assignment_id: str, trade_id: str) -> dict:
        card = self._catalog.get(assignment_id)
        if card is not None:
            return {"id": assignment_id, "strategy": card["strategy"], "timeframe": card["timeframe"]}
        parts = trade_id.split(":")
        return {"id": assignment_id, "strategy": None, "timeframe": parts[2] if len(parts) > 2 else None}

    def _href(self, part: str, query: dict[str, list[str]]) -> str:
        pairs = [(key, value) for key, values in query.items() for value in values]
        return f"{self.base}/{part}" + (f"?{urlencode(pairs, safe=':')}" if pairs else "")

    def _page(self, items, earlier, lower, upper, limit, instrument, edge=None) -> dict:
        query = {key: [value] for key, value in (("from", _iso(lower)), ("instrument", instrument)) if value}
        links = {"self": self._href("trades", {**query, "to": [_iso(upper)], "limit": [str(limit)]})}
        if earlier and edge is not None:
            links["earlier"] = self._href("trades", {**query, "to": [_iso(edge)], "limit": [str(limit)]})
        return {"currency": CURRENCY, "from": _iso(lower), "to": _iso(upper), "items": items,
                "_links": views.links(**links)}


class _Snapshot:
    """Одна транзакция чтения журнала контура: ``None`` внутри ``with`` — журнала нет."""

    def __init__(self, path: Path | None, binding) -> None:
        self._path = path
        self._binding = binding
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _Journal | None:
        if self._path is None or not self._path.is_file():
            return None
        try:
            self._connection = sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            self._connection.execute("BEGIN")
            if self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'").fetchone() is None:
                return None
            return _Journal(self._connection, self._binding)
        except sqlite3.Error as exc:
            self.__exit__(None, None, None)
            raise _unavailable(exc) from None

    def __exit__(self, kind, value, traceback) -> None:
        if self._connection is not None:
            with closing(self._connection):
                self._connection.rollback()
            self._connection = None
        if isinstance(value, sqlite3.Error):
            raise _unavailable(value) from None


class _Journal:
    """Чтение журнала внутри одной транзакции."""

    def __init__(self, connection: sqlite3.Connection, binding) -> None:
        self._connection = connection
        self._binding = binding
        self._names = dict(connection.execute("SELECT ticker, short_name FROM instrument_names").fetchall())
        self._exporter = CsvExporter(connection, Path("trade_event.csv"), Path("trade_summary.csv"), names=self._names)
        self._times = {row[0]: row[1:] for row in connection.execute(_FILL_TIMES).fetchall()}
        self._rows: dict[str, dict] | None = None

    def revision(self) -> int:
        return int(self._connection.execute("SELECT COALESCE(MAX(event_seq), 0) FROM events").fetchone()[0])

    def account(self) -> dict | None:
        cursor = self._connection.execute("SELECT balance, net_realized_pnl, fees, updated_at FROM account LIMIT 1")
        row = cursor.fetchone()
        return None if row is None else dict(zip((column[0] for column in cursor.description), row, strict=True))

    def rows(self) -> dict[str, dict]:
        if self._rows is None:
            cursor = self._connection.execute(_TRADE_ROWS)
            names = tuple(column[0] for column in cursor.description)
            self._rows = {row[0]: dict(zip(names, row, strict=True)) for row in cursor.fetchall()}
        return self._rows

    def closed(self) -> list[dict]:
        """Закрытые сделки с исполненным входом: время, деньги и источники комиссий — без карточек."""
        result = []
        for trade_id, row in self.rows().items():
            opened, closed, sources = self._times.get(trade_id, (None, None, None))
            if row["phase"] != "CLOSED" or opened is None or closed is None:
                continue
            gross, fees = _decimal(row["realized_pnl"]) or _ZERO, _decimal(row["fees"]) or _ZERO
            result.append({"trade_id": trade_id, "instrument": self._names.get(str(row["instrument_id"])),
                           "opened_at": _utc(opened), "closed_at": _utc(closed),
                           "gross": gross, "fees": fees, "net": gross - fees,
                           "fee_sources": tuple((sources or "unknown").split(","))})
        return result

    def closed_card(self, row: dict) -> dict:
        trade = self.rows()[row["trade_id"]]
        card = self._exporter.trade_card(trade)
        entries, exits = card["entries"], card["exits"]
        last = exits[-1]
        summary = card["summary"]
        return {
            **self._head(trade),
            "lots": int(summary["max_quantity"] or 0),
            "entry_price": _price(_average(entries), trade),
            "exit_price": _price(_average(exits), trade),
            "opened_at": _iso(row["opened_at"]),
            "closed_at": _iso(row["closed_at"]),
            "entries": [_fill(fill, trade) for fill in entries],
            "exits": [_exit(fill, trade) for fill in exits],
            "exit": {"kind": exit_kind(last["action_type"], last["code"]), "code": last["code"],
                     "label": reason_label(last["reason"]), "scenario": summary["exit_scenario"]},
            "gross": _money(row["gross"]),
            "fees": _money(row["fees"]),
            "fees_source": _fees_source(row["fee_sources"]),
            "net": _money(row["net"]),
            "result_r": summary["result_r"] or None,
        }

    def positions(self, prices: dict[str, tuple[str, str]]) -> list[dict]:
        items = []
        for trade_id, trade in self.rows().items():
            lots = int(trade["quantity"] or 0)
            if lots <= 0:
                continue
            card = self._exporter.trade_card(trade)
            if card is None:
                continue
            entries, exits = card["entries"], card["exits"]
            entry = _decimal(trade["average_price"]) or _average(entries)
            price, price_at = prices.get(str(trade["instrument_id"]), (None, None))
            last = _decimal(price)
            gross, fees = _decimal(trade["realized_pnl"]) or _ZERO, _decimal(trade["fees"]) or _ZERO
            stop = self._connection.execute(
                "SELECT confirmed_stop, pending_stop FROM protection WHERE trade_id=?", (trade_id,)).fetchone()
            targets = self._connection.execute(
                "SELECT price, planned_quantity, filled_quantity FROM targets WHERE trade_id=? ORDER BY target_index",
                (trade_id,)).fetchall()
            _opened, _closed, sources = self._times.get(trade_id, (None, None, None))
            items.append({
                **self._head(trade),
                "lots": lots,
                "entry_price": _price(entry, trade),
                "last_price": _price(last, trade) if last is not None else None,
                "last_price_at": _iso(_utc(price_at)) if last is not None and price_at else None,
                "stop": _price(_decimal(stop[0]), trade) if stop and stop[0] not in (None, "") else None,
                "stop_pending": _price(_decimal(stop[1]), trade) if stop and stop[1] not in (None, "") else None,
                "targets": [{"price": _price(_decimal(price), trade), "lots": int(planned or 0),
                             "filled": int(filled or 0)} for price, planned, filled in targets],
                "opened_at": _iso(_utc(entries[0]["executed_at"])),
                "entries": [_fill(fill, trade) for fill in entries],
                "exits": [_exit(fill, trade) for fill in exits],
                "phase": _PHASES.get(str(trade["phase"]), "open"),
                "fees": _money(fees),
                "fees_source": _fees_source(tuple((sources or "unknown").split(","))),
                "realized": _money(gross - fees),
                "unrealized": _money(_unrealized(trade, entry, last, lots)),
            })
        items.sort(key=lambda item: (item["opened_at"], item["id"]), reverse=True)
        return items

    def _head(self, trade: dict) -> dict:
        trade_id = str(trade["trade_id"])
        side = str(trade["side"] or "").upper()
        return {"id": trade_ref(trade_id), "binding": self._binding(str(trade["assignment_id"]), trade_id),
                "instrument": self._names.get(str(trade["instrument_id"])),
                "side": "long" if side == "BUY" else "short"}


# --- цена ---------------------------------------------------------------------
def _prices(store) -> dict[str, tuple[str, str]]:
    """Тикер → (закрытие, время открытия) последней завершённой ``1m`` рыночной базы ядра; базы нет — пусто."""
    if store is None:
        return {}
    try:
        series = store.candle_series(source=SOURCE_TBANK)
    except Exception as exc:  # noqa: BLE001 — цены нет, позиции всё равно отдаются
        log.warning("Цены для позиций контура недоступны: %s", type(exc).__name__)
        return {}
    return {str(row["ticker"]): (row["last_close"], row["last_open"]) for row in series
            if row["interval"] == "1m" and row["ticker"] and row["last_close"] is not None}


def _unrealized(trade: dict, entry: Decimal | None, last: Decimal | None, lots: int) -> Decimal | None:
    step, cost = _decimal(trade["price_step"]), _decimal(trade["step_cost"])
    if entry is None or last is None or not step or cost is None:
        return None
    sign = 1 if str(trade["side"] or "").upper() == "BUY" else -1
    return (last - entry) * lots * cost / step * sign


def _unrealized_total(items: list[dict]) -> tuple[Decimal, bool]:
    known = [Decimal(item["unrealized"]) for item in items if item["unrealized"] is not None]
    return sum(known, _ZERO), len(known) == len(items)


# --- кривая -------------------------------------------------------------------
def _curve(start: datetime, trades: list[dict]) -> list[tuple[datetime, Decimal]]:
    """Ноль на начале периода и точка на каждый ``closed_at`` с накопленным нетто."""
    points = [(start, _ZERO)]
    total = _ZERO
    for row in trades:
        total += row["net"]
        if points[-1][0] == row["closed_at"] and len(points) > 1:
            points[-1] = (row["closed_at"], total)
        else:
            points.append((row["closed_at"], total))
    return points


def _drawdown(points: list[tuple[datetime, Decimal]]) -> tuple[Decimal, datetime | None]:
    peak, worst, bottom = _ZERO, _ZERO, None
    for at, net in points:
        peak = max(peak, net)
        if net - peak < worst:
            worst, bottom = net - peak, at
    return worst, bottom


def _thin(points: list[tuple[datetime, Decimal]]) -> list[tuple[datetime, Decimal]]:
    """Не больше ``CURVE_MAX_POINTS`` точек: в каждом интервале времени — первая, последняя, минимум и максимум."""
    if len(points) <= CURVE_MAX_POINTS:
        return points
    buckets = (CURVE_MAX_POINTS - 2) // 4
    first, last = points[0][0], points[-1][0]
    span = (last - first) / buckets or timedelta(microseconds=1)
    groups: dict[int, list[int]] = {}
    for index, (at, _net) in enumerate(points):
        groups.setdefault(min(int((at - first) / span), buckets - 1), []).append(index)
    keep = {0, len(points) - 1}
    for indices in groups.values():
        keep.update((indices[0], indices[-1], min(indices, key=lambda i: points[i][1]),
                     max(indices, key=lambda i: points[i][1])))
    return [points[index] for index in sorted(keep)]


# --- параметры ----------------------------------------------------------------
def _known(query: dict[str, list[str]], allowed: tuple[str, ...]) -> None:
    for key, values in query.items():
        if key not in allowed or len(values) != 1:
            raise _bad(f"Параметры — {', '.join(allowed)}, каждый не больше одного раза.")


def _instrument_filter(query: dict[str, list[str]]) -> str | None:
    value = (query.get("instrument") or [None])[0]
    if value is not None and not value.strip():
        raise _bad("instrument — короткое имя инструмента.")
    return value


def _matches(item: dict, instrument: str | None) -> bool:
    return instrument is None or (item["instrument"] or "").casefold() == instrument.casefold()


def _limit(query: dict[str, list[str]]) -> int:
    raw = (query.get("limit") or [str(LIMIT_DEFAULT)])[0]
    if not raw.isdigit() or not 1 <= int(raw) <= LIMIT_MAX:
        raise _bad(f"limit — целое от 1 до {LIMIT_MAX}.")
    return int(raw)


def _moment(query: dict[str, list[str]], key: str) -> datetime | None:
    raw = (query.get(key) or [None])[0]
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise _bad(f"{key} — момент ISO 8601 с зоной, например 2026-10-31T16:00:00Z.")
    return moment.astimezone(timezone.utc)


def _window(query: dict[str, list[str]], now: datetime) -> tuple[datetime | None, datetime]:
    lower, upper = _moment(query, "from"), _moment(query, "to") or now
    if lower is not None and lower >= upper:
        raise _bad("from должен быть раньше to.")
    return lower, upper


def _period(query: dict[str, list[str]], now: datetime) -> tuple[str, datetime | None, datetime]:
    """Имя и границы периода: торговый день — с последних 19:00 МСК, неделя и месяц — календарные по МСК."""
    if "period" in query:
        if "from" in query or "to" in query:
            raise _bad("period или from/to — не вместе.")
        name = query["period"][0]
        if name not in PERIODS:
            raise _bad(f"period — одно из: {', '.join(PERIODS)}.")
    elif "from" in query or "to" in query:
        lower, upper = _window(query, now)
        return "custom", lower, upper
    else:
        name = "day"
    local = now.astimezone(_MSK)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "day":
        start = local.replace(hour=_DAY_START_HOUR, minute=0, second=0, microsecond=0)
        start = start if start <= local else start - timedelta(days=1)
    elif name == "week":
        start = midnight - timedelta(days=local.weekday())
    elif name == "month":
        start = midnight.replace(day=1)
    else:
        return name, None, now
    return name, start.astimezone(timezone.utc), now


# --- значения -----------------------------------------------------------------
def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_CENT), "f")


def _price(value: Decimal | None, trade: dict) -> str | None:
    """Цена: средние — до знаков шага цены плюс два, без хвостовых нулей."""
    if value is None:
        return None
    step = _decimal(trade.get("price_step"))
    if step:
        value = value.quantize(Decimal(1).scaleb(min(step.normalize().as_tuple().exponent, 0) - 2))
    return format(value.normalize(), "f")


def _average(fills: list[dict]) -> Decimal | None:
    quantity = sum(int(fill["quantity"]) for fill in fills)
    if not quantity:
        return None
    return sum((Decimal(str(fill["price"])) * int(fill["quantity"]) for fill in fills), _ZERO) / quantity


def _fill(fill: dict, trade: dict) -> dict:
    return {"at": _iso(_utc(fill["executed_at"])), "lots": int(fill["quantity"]),
            "price": _price(Decimal(str(fill["price"])), trade)}


def _exit(fill: dict, trade: dict) -> dict:
    return {**_fill(fill, trade), "kind": exit_kind(fill["action_type"], fill["code"]), "code": fill["code"],
            "label": reason_label(fill["reason"])}


def _fees_source(sources) -> str | None:
    found = set(sources)
    if not found:
        return None
    return found.pop() if len(found) == 1 else "mixed"


def _utc(text: object) -> datetime:
    stamp = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat().replace("+00:00", "Z")


def _bad(detail: str) -> TradesError:
    return TradesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", detail)


def _unavailable(exc: BaseException) -> TradesError:
    log.warning("Журнал сделок контура не читается: %s", type(exc).__name__)
    return TradesError(HTTPStatus.SERVICE_UNAVAILABLE, "JOURNAL_UNAVAILABLE",
                       "Журнал сделок контура не читается; подробности — в логе робота.")
