"""Отчёт «Песочница против эмуляции» (add-account-vs-emulation-report): только чтение двух журналов.

Ядро открывает ``trades.sqlite3`` эмуляции и песочницы только на чтение — по
одной транзакции на ответ, как ресурсы торговли контура. Пара — сделки двух
контуров с одинаковым ключом сигнала (``signal_id``: привязка, тикер, ТФ,
бар, сторона). Сделка без пары объясняется причиной из журнала другого
контура. В отчёт входят только привязки песочницы: эмуляция торгует шире.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from urllib.parse import urlencode

from src.bot import control_views as views
from src.bot.trades_view import (ContourTrades, _bad, _decimal, _instrument_filter, _iso, _known, _limit, _period,
                                 _utc)
from src.trade_journal.reasons import reason_label

log = logging.getLogger(__name__)

PARAMS = ("period", "from", "to", "binding", "instrument", "limit")
CURRENCY = "RUB"
TIME_GAP_SECS = 60
NOT_SEEN = "not-seen"
NOT_SEEN_LABEL = "сигнала в контуре не было"
ENTRY_PENDING = "entry-pending"
ENTRY_PENDING_LABEL = "вход ещё не исполнен"
DIFF_LABELS = {
    "entry_price": "цена входа",
    "entry_time": "время входа",
    "lots": "объём",
    "exit_kind": "вид выхода",
    "exit_price": "цена выхода",
    "exit_time": "время выхода",
    "closed_one_side": "закрыта только в одном контуре",
}
_UNFILLED = ("REJECTED", "CANCELLED")
_ENTRY_FILLS = """
SELECT DISTINCT f.trade_id FROM fills f JOIN orders o ON o.order_id = f.order_id
WHERE o.action_type LIKE 'OPEN%' OR o.action_type LIKE 'ADD%'
"""
_REJECTIONS = """
SELECT signal_id, reason FROM calculations
WHERE outcome = 'REJECTED' AND signal_id IS NOT NULL AND signal_id <> ''
ORDER BY created_at, calculation_id
"""
_LAST_OUTCOME = """
SELECT trade_id, payload_json FROM events
WHERE event_type IN ('CANCEL', 'REJECT') AND trade_id IS NOT NULL
ORDER BY event_seq
"""
_FEE_SOURCES = "SELECT trade_id, COALESCE(fee_source, 'unknown'), COUNT(*) FROM fills GROUP BY 1, 2"


def parse_signal(signal_id: str) -> dict:
    """``привязка:тикер:ТФ:бар:сторона``; время бара само содержит двоеточия."""
    parts = signal_id.split(":")
    if len(parts) < 5:
        return {"binding": parts[0] if parts else "", "ticker": parts[1] if len(parts) > 1 else "",
                "timeframe": parts[2] if len(parts) > 2 else None, "side": None}
    return {"binding": parts[0], "ticker": parts[1], "timeframe": parts[2], "side": parts[-1].upper()}


class _Contour:
    """Что отчёту нужно от журнала одного контура — внутри его транзакции чтения."""

    def __init__(self, journal) -> None:
        self.journal = journal
        self.trades: dict[str, dict] = {}       # signal_id → строка сделки
        self.filled: set[str] = set()           # trade_id с исполнением входа
        self.rejections: dict[str, str] = {}    # signal_id → код последнего отказа решения
        self.outcomes: dict[str, str] = {}      # trade_id → причина последней отмены или отказа
        self.fee_sources: dict[str, dict[str, int]] = {}
        if journal is None:
            return
        for row in journal.rows().values():
            signal = str(row.get("signal_id") or row["trade_id"])
            self.trades[signal] = row
        self.filled = {row[0] for row in journal.query(_ENTRY_FILLS)}
        self.rejections = {signal: str(reason or "") for signal, reason in journal.query(_REJECTIONS)}
        for trade_id, payload in journal.query(_LAST_OUTCOME):
            try:
                reason = json.loads(payload).get("reason")
            except (TypeError, ValueError):
                reason = None
            if reason:
                self.outcomes[trade_id] = str(reason)
        for trade_id, source, count in journal.query(_FEE_SOURCES):
            self.fee_sources.setdefault(trade_id, {})[source] = int(count)

    def traded(self, signal: str) -> dict | None:
        """Сделка отчёта по сигналу: строка журнала с исполнением входа."""
        row = self.trades.get(signal)
        return row if row is not None and row["trade_id"] in self.filled else None

    def missing_reason(self, signal: str) -> tuple[str, str]:
        if signal in self.rejections:
            code = self.rejections[signal]
            return code, reason_label(code)
        row = self.trades.get(signal)
        if row is not None and row["trade_id"] not in self.filled:
            if str(row["phase"]) in _UNFILLED:
                code = self.outcomes.get(row["trade_id"]) or str(row["phase"]).lower()
                return code, reason_label(code)
            return ENTRY_PENDING, ENTRY_PENDING_LABEL
        return NOT_SEEN, NOT_SEEN_LABEL


class ContourComparison:
    """``GET /api/v1/sandbox/comparison``."""

    def __init__(self, *, emulation: ContourTrades, sandbox: ContourTrades, members: Callable[[], set[str]],
                 catalog: dict[str, dict], clock: Callable[[], datetime] | None = None) -> None:
        self._emulation = emulation
        self._sandbox = sandbox
        self._members = members
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def report(self, query: dict[str, list[str]]) -> dict:
        _known(query, PARAMS)
        now = self._clock()
        name, lower, upper = _period(query, now)
        limit = _limit(query)
        instrument = _instrument_filter(query)
        binding = (query.get("binding") or [None])[0]
        with self._sandbox._snapshot() as sandbox_journal, self._emulation._snapshot() as emulation_journal:
            sandbox, emulation = _Contour(sandbox_journal), _Contour(emulation_journal)
            in_period = lambda row: (lower is None or _utc(row["created_at"]) >= lower) and \
                _utc(row["created_at"]) < upper  # noqa: E731
            bindings = set(self._members()) | {str(row["assignment_id"]) for row in sandbox.trades.values()
                                               if in_period(row)}
            if binding is not None and binding not in bindings and binding not in self._catalog:
                raise _bad(f"Привязки {binding!r} нет в конфигурации и в песочнице.")
            items = []
            signals = {signal for contour in (sandbox, emulation) for signal, row in contour.trades.items()
                       if contour.traded(signal) is not None and in_period(row)
                       and str(row["assignment_id"]) in bindings}
            for signal in signals:
                item = self._item(signal, sandbox, emulation)
                if binding is not None and item["signal"]["binding"]["id"] != binding:
                    continue
                if instrument is not None and (item["signal"]["instrument"] or "").casefold() != instrument.casefold():
                    continue
                items.append(item)
        items.sort(key=lambda item: (item["signal"]["at"], item["_signal"]), reverse=True)
        summary = _summary(items)
        page = items[:limit]
        for item in items:
            for key in ("_signal", "_fees", "_raw"):
                item.pop(key, None)
        return {"currency": CURRENCY, "as_of": _iso(now), "period": {"name": name, "from": _iso(lower), "to": _iso(upper)},
                "summary": summary, "items": page, "earlier": len(items) > len(page),
                "_links": views.links(self=self._href(query))}

    # --- элементы -------------------------------------------------------------
    def _item(self, signal: str, sandbox: _Contour, emulation: _Contour) -> dict:
        sb_row, em_row = sandbox.traded(signal), emulation.traded(signal)
        row = sb_row or em_row
        journal = (sandbox if sb_row is not None else emulation).journal
        parsed = parse_signal(signal)
        side = str(row["side"] or parsed["side"] or "").upper()
        name = None
        for contour in (sandbox, emulation):
            if contour.journal is not None and name is None:
                name = contour.journal.name(str(row["instrument_id"]))
        item = {
            "_signal": signal,
            "signal": {"binding": journal.binding(str(row["assignment_id"]), str(row["trade_id"])),
                       "instrument": name, "timeframe": parsed["timeframe"],
                       "side": "long" if side == "BUY" else "short",
                       "at": _iso(min(_utc(r["created_at"]) for r in (sb_row, em_row) if r is not None))},
            "emulation": None if em_row is None else emulation.journal.side(str(em_row["trade_id"])),
            "sandbox": None if sb_row is None else sandbox.journal.side(str(sb_row["trade_id"])),
            "_fees": sandbox.fee_sources.get(str(sb_row["trade_id"]), {}) if sb_row is not None else {},
        }
        if sb_row is not None and em_row is not None:
            step = _decimal(sb_row["price_step"]) or _decimal(em_row["price_step"])
            item["kind"] = "pair"
            item["diffs"], raw = _diffs(item["emulation"], item["sandbox"], step, side)
            item["steps_unknown"] = not step
            item["_raw"] = raw
        elif sb_row is not None:
            code, label = emulation.missing_reason(signal)
            item["kind"] = "sandbox_only"
            item["missing_reason"] = {"contour": "emulation", "code": code, "label": label}
        else:
            code, label = sandbox.missing_reason(signal)
            item["kind"] = "emulation_only"
            item["missing_reason"] = {"contour": "sandbox", "code": code, "label": label}
        return item

    def _href(self, query: dict[str, list[str]]) -> str:
        pairs = [(key, value) for key, values in query.items() for value in values]
        return views.SANDBOX_COMPARISON + (f"?{urlencode(pairs, safe=':')}" if pairs else "")


def _diffs(emulation: dict, sandbox: dict, step: Decimal | None, side: str) -> tuple[list[dict], dict]:
    """Расхождения пары и отклонения в шагах для сводки: плюс — песочница хуже по стороне сделки."""
    return _diffs_between("emulation", emulation, "sandbox", sandbox, step, side)


def _diffs_between(ref_name: str, emulation: dict, own_name: str, sandbox: dict, step: Decimal | None,
                   side: str) -> tuple[list[dict], dict]:
    """Расхождения ``own`` от опорного ``ref``: плюс в шагах — ``own`` хуже по стороне сделки."""
    diffs: list[dict] = []
    raw: dict[str, Decimal] = {}
    sign = 1 if side == "BUY" else -1

    def add(code: str, value) -> None:
        diffs.append({"code": code, "label": DIFF_LABELS[code], "value": value})

    em_entry, sb_entry = emulation["entry"], sandbox["entry"]
    if step:
        steps = (Decimal(sb_entry["price"]) - Decimal(em_entry["price"])) * sign / step
        raw["entry"] = steps
        if abs(steps) >= 1:
            add("entry_price", _steps(steps))
    gap = _seconds(em_entry["at"], sb_entry["at"])
    if abs(gap) > TIME_GAP_SECS:
        add("entry_time", gap)
    if em_entry["lots"] != sb_entry["lots"]:
        add("lots", {ref_name: em_entry["lots"], own_name: sb_entry["lots"]})
    em_exit, sb_exit = emulation["exit"], sandbox["exit"]
    if em_exit is not None and sb_exit is not None:
        if em_exit["kind"] != sb_exit["kind"]:
            add("exit_kind", {ref_name: em_exit["kind"], own_name: sb_exit["kind"]})
        elif step:
            steps = (Decimal(em_exit["price"]) - Decimal(sb_exit["price"])) * sign / step
            raw["exit"] = steps
            if abs(steps) >= 1:
                add("exit_price", _steps(steps))
        gap = _seconds(em_exit["at"], sb_exit["at"])
        if abs(gap) > TIME_GAP_SECS:
            add("exit_time", gap)
    elif (em_exit is None) != (sb_exit is None):
        add("closed_one_side", ref_name if em_exit is not None else own_name)
    return diffs, raw


def _steps(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    return format(value.normalize(), "f") if value else "0"


def _seconds(emulation_at: str, sandbox_at: str) -> int:
    return int(round((_utc(sandbox_at) - _utc(emulation_at)).total_seconds()))


def _summary(items: list[dict]) -> dict:
    pairs = [item for item in items if item["kind"] == "pair"]
    missing: dict[tuple[str, str], dict] = {}
    for item in items:
        reason = item.get("missing_reason")
        if reason is None:
            continue
        entry = missing.setdefault((reason["contour"], reason["code"]), {**reason, "count": 0})
        entry["count"] += 1
    diffs: dict[str, int] = {}
    entry_steps, exit_steps = [], []
    for item in pairs:
        for diff in item["diffs"]:
            diffs[diff["code"]] = diffs.get(diff["code"], 0) + 1
        raw = item.get("_raw", {})
        if "entry" in raw:
            entry_steps.append(raw["entry"])
        if "exit" in raw:
            exit_steps.append(raw["exit"])
    closed = [item for item in pairs if item["emulation"]["state"] == "closed" and item["sandbox"]["state"] == "closed"]
    fees = {"broker": 0, "estimated": 0}
    for item in items:
        for source, count in item.get("_fees", {}).items():
            fees["broker" if source == "broker" else "estimated"] += count
    return {
        "pairs": len(pairs),
        "emulation_only": sum(1 for item in items if item["kind"] == "emulation_only"),
        "sandbox_only": sum(1 for item in items if item["kind"] == "sandbox_only"),
        "missing_reasons": sorted(missing.values(), key=lambda entry: (-entry["count"], entry["contour"],
                                                                        entry["code"])),
        "diffs": diffs,
        "entry_steps": _stats(entry_steps),
        "exit_steps": _stats(exit_steps),
        "paired_result": {
            "count": len(closed),
            "emulation": _side_total(closed, "emulation"),
            "sandbox": _side_total(closed, "sandbox"),
        },
        "sandbox_fees": fees,
    }


def _stats(values: list[Decimal]) -> dict | None:
    if not values:
        return None
    return {"count": len(values), "mean": _steps(sum(values, Decimal(0)) / len(values)),
            "max_abs": _steps(max(abs(value) for value in values))}


def _side_total(items: list[dict], contour: str) -> dict:
    total_r = sum((Decimal(str(item[contour]["result_r"])) for item in items
                   if item[contour]["result_r"] not in (None, "")), Decimal(0))
    per_lot = sum((Decimal(item[contour]["net_per_lot"]) for item in items
                   if item[contour]["net_per_lot"] is not None), Decimal(0))
    return {"result_r": format(total_r.quantize(Decimal("0.01")), "f"),
            "net_per_lot": format(per_lot.quantize(Decimal("0.01")), "f")}


# --- «Счёт» против песочницы и эмуляции (enable-live-trading) -------------------------------

OTHERS = ("sandbox", "emulation")   # стороны, с которыми сравнивается контур вида «счёт»; своя — ``own``


class AccountComparison:
    """``GET /api/v1/<контур>/comparison``: сигналы привязок контура вида «счёт», три стороны, расхождения от счёта.

    ``own`` — имя своей стороны и ключей ``<own>_vs_sandbox``, ``<own>_vs_emulation`` (add-finam-contour):
    у «Счёта» ``account``; базовый путь ссылок — ``base``.
    """

    def __init__(self, *, account: ContourTrades, sandbox: ContourTrades, emulation: ContourTrades,
                 members: Callable[[], set[str]], catalog: dict[str, dict],
                 clock: Callable[[], datetime] | None = None, own: str = "account", base: str = views.ACCOUNT) -> None:
        self._own, self._base = own, base
        self.sides = (own, *OTHERS)
        self._trades = {own: account, "sandbox": sandbox, "emulation": emulation}
        self._members = members
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def report(self, query: dict[str, list[str]]) -> dict:
        _known(query, PARAMS)
        now = self._clock()
        name, lower, upper = _period(query, now)
        limit = _limit(query)
        instrument = _instrument_filter(query)
        binding = (query.get("binding") or [None])[0]
        trades = self._trades
        own = self._own
        with trades[own]._snapshot() as account_journal, trades["sandbox"]._snapshot() as sandbox_journal, \
                trades["emulation"]._snapshot() as emulation_journal:
            contours = {own: _Contour(account_journal), "sandbox": _Contour(sandbox_journal),
                        "emulation": _Contour(emulation_journal)}
            in_period = lambda row: (lower is None or _utc(row["created_at"]) >= lower) and \
                _utc(row["created_at"]) < upper  # noqa: E731
            bindings = set(self._members()) | {str(row["assignment_id"]) for row in contours[own].trades.values()
                                               if in_period(row)}
            if binding is not None and binding not in bindings and binding not in self._catalog:
                raise _bad(f"Привязки {binding!r} нет в конфигурации и на счёте.")
            signals = {signal for contour in contours.values() for signal, row in contour.trades.items()
                       if contour.traded(signal) is not None and in_period(row)
                       and str(row["assignment_id"]) in bindings}
            items = []
            for signal in signals:
                item = self._item(signal, contours)
                if binding is not None and item["signal"]["binding"]["id"] != binding:
                    continue
                if instrument is not None and (item["signal"]["instrument"] or "").casefold() != instrument.casefold():
                    continue
                items.append(item)
        items.sort(key=lambda item: (item["signal"]["at"], item["_signal"]), reverse=True)
        summary = _account_summary(items, own)
        page = items[:limit]
        for item in items:
            for key in ("_signal", "_raw"):
                item.pop(key, None)
        href = self._base + "/comparison"
        pairs = [(key, value) for key, values in query.items() for value in values]
        return {"currency": CURRENCY, "as_of": _iso(now), "period": {"name": name, "from": _iso(lower), "to": _iso(upper)},
                "summary": summary, "items": page, "earlier": len(items) > len(page),
                "_links": views.links(self=href + (f"?{urlencode(pairs, safe=':')}" if pairs else ""))}

    def _item(self, signal: str, contours: dict[str, _Contour]) -> dict:
        sides, own = self.sides, self._own
        rows = {name: contours[name].traded(signal) for name in sides}
        first = next(name for name in sides if rows[name] is not None)
        row, journal = rows[first], contours[first].journal
        parsed = parse_signal(signal)
        side = str(row["side"] or parsed["side"] or "").upper()
        instrument = next((contours[name].journal.name(str(row["instrument_id"])) for name in sides
                           if contours[name].journal is not None), None)
        item = {
            "_signal": signal,
            "signal": {"binding": journal.binding(str(row["assignment_id"]), str(row["trade_id"])),
                       "instrument": instrument, "timeframe": parsed["timeframe"],
                       "side": "long" if side == "BUY" else "short",
                       "at": _iso(min(_utc(r["created_at"]) for r in rows.values() if r is not None))},
            "missing": {},
            "_raw": {},
        }
        for name in sides:
            item[name] = None if rows[name] is None else contours[name].journal.side(str(rows[name]["trade_id"]))
            if rows[name] is None:
                code, label = contours[name].missing_reason(signal)
                item["missing"][name] = {"code": code, "label": label}
        for other in OTHERS:
            key = f"{own}_vs_{other}"
            if rows[own] is None or rows[other] is None:
                item[key] = None
                continue
            step = _decimal(rows[own]["price_step"]) or _decimal(rows[other]["price_step"])
            diffs, raw = _diffs_between(other, item[other], own, item[own], step, side)
            item[key] = {"diffs": diffs, "steps_unknown": not step}
            item["_raw"][other] = raw
        return item


def _account_summary(items: list[dict], own: str = "account") -> dict:
    sides = (own, *OTHERS)
    trades = {name: sum(1 for item in items if item[name] is not None) for name in sides}
    missing: dict[tuple[str, str], dict] = {}
    for item in items:
        for contour, reason in item["missing"].items():
            entry = missing.setdefault((contour, reason["code"]), {"contour": contour, **reason, "count": 0})
            entry["count"] += 1
    comparisons = {}
    for other in OTHERS:
        key = f"{own}_vs_{other}"
        paired = [item for item in items if item[key] is not None]
        diffs: dict[str, int] = {}
        entry_steps, exit_steps = [], []
        for item in paired:
            for diff in item[key]["diffs"]:
                diffs[diff["code"]] = diffs.get(diff["code"], 0) + 1
            raw = item["_raw"].get(other, {})
            if "entry" in raw:
                entry_steps.append(raw["entry"])
            if "exit" in raw:
                exit_steps.append(raw["exit"])
        comparisons[key] = {"pairs": len(paired), "diffs": diffs, "entry_steps": _stats(entry_steps),
                            "exit_steps": _stats(exit_steps)}
    totals = {name: _side_total([item for item in items if item[name] is not None and item[name]["state"] == "closed"],
                                name) for name in sides}
    return {"trades": trades, **comparisons, "closed_result": totals,
            "missing_reasons": sorted(missing.values(), key=lambda entry: (-entry["count"], entry["contour"],
                                                                            entry["code"]))}
