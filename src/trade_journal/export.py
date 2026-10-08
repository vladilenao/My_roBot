"""Atomic CSV projections of the SQLite trade journal.

Проекции рассчитаны на пользователя: без внутренних ID, с короткими именами
контрактов (NG-10.26), московским временем и описанием событий фразами.
Точные технические данные остаются в SQLite-журнале (source of truth).
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import logging.handlers
import os
from pathlib import Path
from tempfile import mkstemp
from typing import Mapping

from src.trade_management.lifecycle import lifecycle_label

log = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

JOURNAL_COLUMNS = (
    "occurred_at", "contract", "side", "event", "quantity", "price",
    "position_qty_before", "position_qty_after", "avg_price_before", "avg_price_after", "reason",
)
POSITIONS_COLUMNS = (
    "contract", "direction", "status", "entry_at", "exit_at", "duration",
    "planned_entry", "planned_stop", "planned_tp1", "initial_quantity", "added_quantity",
    "max_quantity", "average_entry", "exits", "average_exit", "final_reason", "exit_scenario",
    "gross_pnl", "fees", "net_pnl", "pnl_units", "planned_risk", "initial_risk", "result_r", "mae_r", "mfe_r",
    "cost_coverage", "excursion_coverage", "excursion_definition", "algorithm_version",
)

# Человекочитаемые русскоязычные заголовки CSV-проекций. Позиции соответствуют
# внутренним именам в *_COLUMNS (шаг цены/стоимость шага/ГО берутся из метаданных
# контракта брокера, а не из SQLite).
JOURNAL_HEADERS = (
    "Время (МСК)",
    "Контракт",
    "Направление",
    "Событие",
    "Количество",
    "Цена",
    "Объем позиции до",
    "Объем позиции после",
    "Средняя цена до",
    "Средняя цена после",
    "Причина",
)
POSITIONS_HEADERS = (
    "Контракт",
    "Направление",
    "Статус",
    "Время входа",
    "Время выхода",
    "Длительность",
    "План входа",
    "План стопа",
    "План TP1",
    "Начальный объем",
    "Добрано",
    "Макс. объем",
    "Средняя входа",
    "Выходы",
    "Средняя выхода",
    "Финальная причина",
    "Сценарий выхода",
    "Gross PnL",
    "Комиссия",
    "Net PnL",
    "Ед. PnL",
    "Плановый риск (₽)",
    "Initial Risk (₽)",
    "Result (R)",
    "MAE (R)",
    "MFE (R)",
    "Полнота издержек",
    "Полнота экстремумов",
    "Определение экстремумов",
    "Версия алгоритма",
)

_SIDE_LABELS = {"BUY": "Покупка", "SELL": "Продажа"}
_PHASE_LABELS = {
    "PLANNED": "план готов",
    "ENTRY_PENDING": "вход ожидается",
    "OPEN": "открыта",
    "BUILDING": "набор позиции",
    "REDUCING": "частичное закрытие",
    "CLOSED": "закрыта",
    "CANCELLED": "отменена",
}
_STATUS_LABELS = {
    "ACK": "Заявка принята",
    "FILL": "Исполнено",
    "PARTIAL": "Исполнено частично",
    "REJECT": "Заявка отклонена",
    "CANCEL": "Заявка отменена",
    "EXPIRED": "Заявка истекла",
}
_ACTION_LABELS = {
    "OPEN": "открытие позиции",
    "ADD": "добор позиции",
    "REDUCE": "частичное закрытие",
    "CLOSE": "закрытие позиции",
    "STOP": "стоп",
}

REASON_LABELS = {
    "levels-profitable-add": "прибыльный добор по уровням",
    "atr-profitable-advance": "добор после продвижения по ATR",
    "ma-cloud-profitable-retest": "прибыльный ретест облака",
    "pattern-profitable-same-formation": "добор по той же формации",
    "cost-aware-break-even": "безубыток всей сделки",
    "atr-trailing-stop": "трейлинг по ATR",
    "STOP": "стоп", "CLOSE": "закрытие", "REDUCE": "частичный выход",
    "next-bar": "вход перенесён на следующий бар",
    "entry-timeout": "вход не исполнен в отведённое время",
    "contract-expiring": "контракт истекает",
    "profile-entry": "вход по профилю",
    "protective": "стоп",
    "tp": "цель по цене",
    "risk-cap": "лимит риска",
    "duplicate-signal": "дублирующий сигнал",
    "opposite-exposure": "встречная позиция",
    "opposite-signal-management": "управление по встречному сигналу",
    "raw-opposite-signal": "встречный сигнал",
    "ma40-opposite-close": "цена ушла за MA40",
    "ma10-or-cloud-partial-exit": "частичный выход по MA10/облаку",
    "factual-increase-fill-invalidates-protection": "добор нарушил защиту",
    "factual-increase-fill-violates-risk": "добор нарушил лимиты риска",
    "late-increase-fill-after-cancel": "компенсация позднего добора",
    "clearing": "клиринг",
    "ttl": "истёк срок заявки",
    "close-not-confirmed": "закрытие не подтверждено",
    "filter-rejected": "сигнал отфильтрован",
    "no-contract-meta": "нет метаданных контракта",
    "insufficient-history": "недостаточно истории",
    "stale-state-revision": "устарела ревизия состояния",
    "trade-not-open": "позиция не открыта",
    "trade-already-open": "позиция уже открыта",
    "unknown-trade": "неизвестная сделка",
    "unknown-target": "неизвестная цель",
    "limit-entry-required": "вход только по лимитной заявке",
    "quantity-non-positive": "недопустимое количество",
    "quantity-exceeds-trade-remainder": "количество больше остатка позиции",
    "quantity-exceeds-target-remainder": "количество больше остатка цели",
    "unsupported-command": "неподдерживаемая команда",
}


def reason_label(code: object) -> str:
    """Перевести код причины в русскую фразу; неизвестный код — как есть."""
    text = str(code or "")
    if text.startswith("tp:"):
        return REASON_LABELS["tp"]
    return REASON_LABELS.get(text, text)


class CsvExporter:
    """Exports both user-facing projections from one read-only SQLite snapshot."""

    def __init__(
        self,
        connection,
        journal_path: Path,
        positions_path: Path,
        contracts: Mapping[str, object] | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        self._connection = connection
        self._journal_path = journal_path
        self._positions_path = positions_path
        self._contracts: dict[str, object] = dict(contracts or {})
        self._names: dict[str, str] = dict(names or {})

    def set_contracts(self, contracts: Mapping[str, object]) -> None:
        """Provide contract metadata (price step, step cost, margin) for cards."""
        self._contracts = dict(contracts or {})

    def set_names(self, names: Mapping[str, str]) -> None:
        """Provide ticker -> short contract name (NGV6 -> NG-10.26) for outputs."""
        self._names = dict(names or {})

    def export(self) -> bool:
        """Replace both projections, retaining prior complete files on any failure."""
        revision, journal_rows, position_rows = self._snapshot()
        files: list[tuple[Path, Path]] = []
        try:
            files = [
                (self._write_temp(self._journal_path, JOURNAL_COLUMNS, JOURNAL_HEADERS, journal_rows),
                 self._journal_path),
                (self._write_temp(self._positions_path, POSITIONS_COLUMNS, POSITIONS_HEADERS, position_rows),
                 self._positions_path),
            ]
            errors: list[OSError] = []
            for temporary, destination in files:
                try:
                    os.replace(temporary, destination)
                except OSError as error:
                    errors.append(error)
            if errors:
                raise errors[0]
        except OSError as error:
            for temporary, _ in files:
                temporary.unlink(missing_ok=True)
            self._record_failure(revision, error)
            log.error("CSV export revision %s failed: %s", revision, error)
            return False
        self._record_success(revision)
        return True

    def _snapshot(self) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
        self._connection.execute("BEGIN")
        try:
            revision = self._connection.execute(
                "SELECT required_revision FROM export_state WHERE export_id = 1"
            ).fetchone()[0]
            journal_rows = []
            replay: dict[str, tuple[int, Decimal | None]] = {}
            for row in self._rows(
                """
                SELECT events.event_seq, events.event_id, events.occurred_at, events.event_type,
                       events.payload_json, trades.trade_id, trades.instrument_id, trades.side,
                       orders.action_type, orders.quantity AS order_quantity,
                       orders.requested_price AS order_price, fills.quantity AS fill_quantity,
                       fills.price AS fill_price
                FROM events
                LEFT JOIN trades ON trades.trade_id = events.trade_id
                LEFT JOIN orders ON orders.order_id = events.order_id
                LEFT JOIN fills ON fills.execution_id = events.event_id
                ORDER BY events.event_seq
                """
            ):
                payload = self._payload(row.get("payload_json"))
                trade_id = str(row.get("trade_id") or "")
                before_qty, before_avg = replay.get(trade_id, (0, None))
                quantity = row.get("fill_quantity") or payload.get("quantity") or row.get("order_quantity") or ""
                price = row.get("fill_price") or payload.get("price") or row.get("order_price") or ""
                actual_fill = row.get("fill_quantity") or payload.get("quantity")
                after_qty, after_avg = (
                    self._replay_fill(
                        before_qty, before_avg, str(row.get("action_type") or ""), actual_fill, price
                    )
                    if actual_fill else (before_qty, before_avg)
                )
                if actual_fill:
                    replay[trade_id] = (after_qty, after_avg)
                display_action = str(row.get("action_type") or "")
                if display_action.startswith("TARGET:"):
                    index = self._connection.execute("SELECT target_index FROM targets WHERE trade_id=? AND target_id=?",
                                                     (trade_id, display_action.partition(":")[2])).fetchone()
                    display_action = f"TARGET:TP{index[0]+1}" if index else "TARGET:"
                journal_rows.append({
                    "occurred_at": self._msk(row.get("occurred_at")),
                    "contract": self._display_contract(str(row.get("instrument_id") or "")),
                    "side": _SIDE_LABELS.get(str(row.get("side") or "").upper(), ""),
                    "event": self._describe_event(
                        str(row.get("event_type") or ""), display_action
                    ),
                    "quantity": quantity,
                    "price": price,
                    "position_qty_before": before_qty if before_qty else "",
                    "position_qty_after": after_qty if after_qty else "",
                    "avg_price_before": self._money(before_avg) if before_avg is not None else "",
                    "avg_price_after": self._money(after_avg) if after_avg is not None else "",
                    "reason": reason_label(payload.get("reason")),
                })
            position_rows = []
            for row in self._rows(
                """
SELECT trades.trade_id, trades.instrument_id, positions.side, trades.created_at, trades.phase,
       trades.plan_json, trades.price_step, trades.step_cost,
       positions.quantity, positions.average_price,
       positions.realized_pnl, positions.fees, positions.updated_at
FROM positions
JOIN trades ON trades.trade_id = positions.trade_id
ORDER BY positions.trade_id
                """
            ):
                ticker = str(row.get("instrument_id") or "")
                meta = self._contracts.get(ticker)
                position_rows.append(self._summary_row(row, meta))
        finally:
            self._connection.rollback()
        return revision, journal_rows, position_rows

    @staticmethod
    def _replay_fill(
        quantity_before: int,
        average_before: Decimal | None,
        action_type: str,
        quantity: object,
        price: object,
    ) -> tuple[int, Decimal | None]:
        """Apply one confirmed fill to the lightweight event projection state."""
        try:
            qty = int(quantity)
            fill_price = Decimal(str(price))
        except (TypeError, ValueError, InvalidOperation):
            return quantity_before, average_before
        action = action_type.upper().split(":", 1)[0]
        if qty <= 0 or fill_price <= 0:
            return quantity_before, average_before
        if action in {"OPEN", "ADD"}:
            total = quantity_before + qty
            weighted = fill_price if average_before is None else (
                average_before * quantity_before + fill_price * qty
            ) / total
            return total, weighted
        if action in {"REDUCE", "CLOSE", "TARGET", "STOP"}:
            remaining = max(0, quantity_before - qty)
            return remaining, average_before if remaining else None
        return quantity_before, average_before

    def _summary_row(self, row: Mapping[str, object], meta: object | None) -> dict[str, object]:
        """Build one trader card from normalized fills and the immutable plan."""
        trade_id = str(row["trade_id"])
        plan = self._payload(row.get("plan_json"))
        side = str(row.get("side") or "").upper()
        entries, exits = self._fills(trade_id)
        first_entry = entries[0] if entries else None
        last_exit = exits[-1] if exits else None
        entry_qty = sum(int(fill["quantity"]) for fill in entries[:1])
        added_qty = sum(int(fill["quantity"]) for fill in entries[1:])
        max_qty = 0
        running = 0
        for fill in entries + exits:
            running += int(fill["quantity"]) if fill["kind"] == "entry" else -int(fill["quantity"])
            max_qty = max(max_qty, running)
        entry_value = sum((Decimal(str(fill["price"])) * int(fill["quantity"]) for fill in entries), Decimal("0"))
        exit_value = sum((Decimal(str(fill["price"])) * int(fill["quantity"]) for fill in exits), Decimal("0"))
        entry_total = sum(int(fill["quantity"]) for fill in entries)
        exit_total = sum(int(fill["quantity"]) for fill in exits)
        average_entry = entry_value / entry_total if entry_total else None
        average_exit = exit_value / exit_total if exit_total else None
        initial_risk = self._initial_risk(trade_id)
        point_value = self._point_value(row.get("price_step"), row.get("step_cost"))
        realized_risk = self._realized_initial_risk(
            plan, average_entry, point_value, max_qty,
        )
        version = plan.get("algorithm_version", "legacy-v1")
        measurement = self._connection.execute("SELECT initial_stop_distance,max_quantity,coverage FROM trade_measurements WHERE trade_id=?", (trade_id,)).fetchone()
        if version == "economics-v2":
            max_qty = measurement[1] if measurement else max_qty
            economics = plan.get("economics") or {}
            initial_risk = Decimal(economics["risk_amount"]) if economics.get("risk_amount") is not None else None
            realized_risk = (Decimal(measurement[0])*point_value*max_qty
                             if measurement and measurement[0] is not None and point_value and max_qty > 0 else
                             None)
        gross = Decimal(str(row.get("realized_pnl") or "0"))
        fees = Decimal(str(row.get("fees") or "0"))
        net = gross - fees
        exit_reasons = [str(fill["reason"]) for fill in exits if fill["reason"]]
        units = self._pnl_units(row.get("price_step"), row.get("step_cost"))
        return {
            "trade_id": trade_id,
            "contract": self._display_contract(str(row.get("instrument_id") or "")),
            "direction": "LONG" if side == "BUY" else "SHORT" if side == "SELL" else "",
            "status": "частично закрыта" if exits and row.get("quantity", 0) > 0 else lifecycle_label(str(row.get("phase") or ""), quantity=int(row.get("quantity") or 0)),
            "entry_at": self._msk(first_entry["executed_at"]) if first_entry else "",
            "exit_at": self._msk(last_exit["executed_at"]) if last_exit else "",
            "duration": self._duration(first_entry, last_exit) if row.get("quantity") == 0 else "",
            "planned_entry": plan.get("reference_entry") or "",
            "planned_stop": plan.get("stop_price") or "",
            "planned_tp1": plan["targets"][0].get("price", self._first_target(trade_id)) if plan.get("targets") else "",
            "initial_quantity": entry_qty or "",
            "added_quantity": added_qty or "",
            "max_quantity": max_qty or "",
            "average_entry": self._money(average_entry) if average_entry is not None else "",
            "exits": self._format_exits(exits),
            "average_exit": self._money(average_exit) if average_exit is not None else "",
            "final_reason": reason_label(exit_reasons[-1]) if row.get("quantity") == 0 and exit_reasons else "",
            "exit_scenario": self._exit_scenario(exits, int(row.get("quantity") or 0)),
            "gross_pnl": self._money(gross) if first_entry else "",
            "fees": self._money(-fees) if first_entry else "",
            "net_pnl": self._money(net) if first_entry else "",
            "pnl_units": units,
            "planned_risk": self._money(initial_risk) if initial_risk is not None else "",
            "initial_risk": self._money(realized_risk) if realized_risk is not None else "",
            "result_r": self._ratio(net, realized_risk),
            "mae_r": self._extreme_r(
                trade_id, average_entry, realized_risk, side, adverse=True,
                point_value=point_value, max_qty=max_qty,
            ),
            "mfe_r": self._extreme_r(
                trade_id, average_entry, realized_risk, side, adverse=False,
                point_value=point_value, max_qty=max_qty,
            ),
            "cost_coverage": self._cost_coverage(trade_id),
            "excursion_coverage": {"complete": "полные доступные наблюдения", "partial": "частичные", "unavailable": "нет наблюдений"}.get(
                measurement[2] if version == "economics-v2" and measurement else "partial" if entries and version == "legacy-v1" else "unavailable"),
            "excursion_definition": "holding-bars-v2" if version == "economics-v2" else "Fills-bounded Excursion Metrics",
            "algorithm_version": version,
        }

    def _cost_coverage(self, trade_id):
        sources = {r[0] for r in self._connection.execute("SELECT DISTINCT fee_source FROM fills WHERE trade_id=?", (trade_id,))}
        if not sources or sources == {"unknown"}:
            return "неизвестны"
        if "unknown" in sources:
            return "частично известны"
        return "брокерские суммы" if sources == {"broker"} else "конфигурационная оценка" if sources == {"configured"} else "смешанные источники"

    def _fills(self, trade_id: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        entries: list[dict[str, object]] = []
        exits: list[dict[str, object]] = []
        for row in self._rows(
            """
            SELECT f.quantity, f.price, f.executed_at, COALESCE(e.payload_json, '{}') AS payload_json,
                   o.action_type
            FROM fills f JOIN orders o ON o.order_id = f.order_id
            LEFT JOIN events e ON e.event_id = f.execution_id
            WHERE f.trade_id = ? ORDER BY f.executed_at, f.fill_id
            """, (trade_id,)
        ):
            action = str(row["action_type"] or "").upper().split(":", 1)[0]
            payload = self._payload(row["payload_json"])
            value = dict(row)
            value["kind"] = "entry" if action in {"OPEN", "ADD"} else "exit"
            value["reason"] = payload.get("reason") or self._action_reason(str(row["action_type"]))
            if action == "TARGET":
                target_id = str(row["action_type"]).partition(":")[2]
                index = self._connection.execute("SELECT target_index FROM targets WHERE trade_id=? AND target_id=?", (trade_id, target_id)).fetchone()
                value["reason"] = f"TP{index[0]+1}" if index else "цель"
            (entries if value["kind"] == "entry" else exits).append(value)
        return entries, exits

    def _first_target(self, trade_id: str) -> object:
        row = self._connection.execute(
            "SELECT price FROM targets WHERE trade_id = ? ORDER BY target_index LIMIT 1", (trade_id,)
        ).fetchone()
        return row[0] if row else ""

    def _initial_risk(self, trade_id: str) -> Decimal | None:
        row = self._connection.execute(
            "SELECT original_risk_amount FROM reservations WHERE trade_id = ? ORDER BY created_at LIMIT 1",
            (trade_id,),
        ).fetchone()
        return Decimal(str(row[0])) if row and row[0] not in (None, "") else None

    @staticmethod
    def _duration(first: Mapping[str, object] | None, last: Mapping[str, object] | None) -> str:
        if not first or not last:
            return ""
        delta = datetime.fromisoformat(str(last["executed_at"])) - datetime.fromisoformat(str(first["executed_at"]))
        return f"{int(delta.total_seconds() // 60)} мин"

    @staticmethod
    def _action_reason(action: str) -> str:
        raw, _, target = action.partition(":")
        if raw == "TARGET":
            return f"TP{target}" if target else "TP"
        return raw

    @classmethod
    def _format_exits(cls, exits: list[dict[str, object]]) -> str:
        return "; ".join(
            f"{reason_label(fill['reason'])}: {fill['quantity']} @{cls._money(fill['price'])}" for fill in exits
        )

    @staticmethod
    def _exit_scenario(exits: list[dict[str, object]], remaining: int) -> str:
        if not exits or remaining:
            return ""
        if len(exits) == 1:
            return reason_label(exits[0]["reason"])
        return " + ".join(
            f"{reason_label(fill['reason'])} (часть)" if index < len(exits) - 1 else f"{reason_label(fill['reason'])} (остаток)"
            for index, fill in enumerate(exits)
        )

    @staticmethod
    def _ratio(value: Decimal, divisor: Decimal | None) -> str:
        return format(value / divisor, ".2f") if divisor and divisor != 0 else ""

    def _extreme_r(
        self, trade_id: str, entry: Decimal | None, risk: Decimal | None, side: str, *, adverse: bool,
        point_value: Decimal | None, max_qty: int,
    ) -> str:
        """How far the position went against or with the trade, in initial risk.

        The excursion is measured in money, not in price points: a point on one
        contract is worth its own step cost, so comparing a raw price distance
        between two different instruments means nothing.
        """
        if entry is None or not risk or risk == 0 or not point_value or max_qty <= 0:
            return ""
        values: list[Decimal] = []
        version = self._payload(self._connection.execute("SELECT plan_json FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0]).get("algorithm_version", "legacy-v1")
        key = "low" if (side == "BUY") == adverse else "high"
        if version == "economics-v2":
            for row in self._rows("SELECT low,high,observed_price FROM trade_market_observations WHERE trade_id=?", (trade_id,)):
                for column in (key, "observed_price"):
                    if row[column] is not None:
                        values.append(Decimal(row[column]))
        else:
            for row in self._rows(
            "SELECT payload_json FROM events WHERE trade_id = ? "
            "AND event_type IN ('FILL', 'PARTIAL') ORDER BY event_seq", (trade_id,),
            ):
                payload = self._payload(row["payload_json"])
                for column in (key, "price"):
                    if payload.get(column) is not None:
                        values.append(Decimal(str(payload[column])))
                        break
        if not values:
            return ""
        favorable = (max(values) - entry) if side == "BUY" else (entry - min(values))
        unfavorable = (entry - min(values)) if side == "BUY" else (max(values) - entry)
        result = unfavorable if adverse else favorable
        return format(max(Decimal(0), result) * point_value * max_qty / risk, ".2f")

    def _realized_initial_risk(
        self, plan: Mapping[str, object], average_entry: Decimal | None,
        point_value: Decimal | None, max_qty: int,
    ) -> Decimal | None:
        """Money at risk from the price actually paid to the stop set for it.

        The stop distance the plan admitted is kept, but it is re-measured from
        the average the position actually holds: a gapped entry pays a different
        price and is judged against its own stop rather than the one it was
        admitted on. Returns ``None`` when the contract's step cost is unknown,
        which keeps a comparison across instruments honest instead of scoring a
        price distance in one contract's points against another's rubles.
        """
        if average_entry is None or not point_value or max_qty <= 0:
            return None
        try:
            distance = abs(Decimal(str(plan["reference_entry"])) - Decimal(str(plan["stop_price"])))
        except (KeyError, TypeError, InvalidOperation):
            return None
        if distance == 0:
            return None
        return distance * point_value * max_qty

    def _rows(self, query: str, parameters: tuple[object, ...] = ()) -> list[dict[str, object]]:
        cursor = self._connection.execute(query, parameters)
        names = tuple(column[0] for column in cursor.description)
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _closing_fills(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for row in self._rows(
            """
            SELECT f.trade_id,
                   SUM(f.price * f.quantity) * 1.0 / NULLIF(SUM(f.quantity), 0) AS exit_price,
                   MAX(f.executed_at) AS exit_at
            FROM fills f
            JOIN orders o ON o.order_id = f.order_id
            WHERE o.action_type IN ('REDUCE', 'CLOSE', 'STOP') OR o.action_type LIKE 'TARGET:%'
            GROUP BY f.trade_id
            """
        ):
            trade_id = str(row.get("trade_id") or "")
            if trade_id:
                result[trade_id] = {"exit_price": row.get("exit_price"), "exit_at": row.get("exit_at")}
        return result

    def _exit_reasons(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in self._rows(
            """
            SELECT e.trade_id, e.payload_json
            FROM events e
            JOIN orders o ON o.order_id = e.order_id
            WHERE o.action_type IN ('REDUCE', 'CLOSE', 'STOP') OR o.action_type LIKE 'TARGET:%'
            ORDER BY e.event_seq DESC
            """
        ):
            trade_id = str(row.get("trade_id") or "")
            if not trade_id or trade_id in result:
                continue
            reason = self._payload(row.get("payload_json")).get("reason") or ""
            result[trade_id] = str(reason)
        return result

    def _entry_risk(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in self._rows(
            """
            SELECT r.trade_id, r.original_risk_amount, o.created_at
            FROM reservations r
            JOIN orders o ON o.order_id = r.order_id
            ORDER BY o.created_at
            """
        ):
            trade_id = str(row.get("trade_id") or "")
            if not trade_id or trade_id in result:
                continue
            result[trade_id] = str(row.get("original_risk_amount") or "")
        return result

    @staticmethod
    def _payload(text: object) -> dict[str, object]:
        if not text:
            return {}
        try:
            value = json.loads(str(text))
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _msk(text: object) -> str:
        if not text:
            return ""
        stamp = datetime.fromisoformat(str(text))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(_MSK).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _money(text: object) -> str:
        if text in (None, ""):
            return ""
        try:
            value = Decimal(str(text))
        except InvalidOperation:
            return str(text)
        return format(value.quantize(Decimal("0.01")), "f")

    @staticmethod
    def _point_value(price_step: object, step_cost: object) -> Decimal | None:
        """Rubles one price step is worth, or ``None`` when the snapshot lacks it."""
        if price_step in (None, "") or step_cost in (None, ""):
            return None
        try:
            step, cost = Decimal(str(price_step)), Decimal(str(step_cost))
        except InvalidOperation:
            return None
        return cost/step if all(value.is_finite() and value > 0 for value in (step, cost)) else None

    @staticmethod
    def _pnl_units(price_step: object, step_cost: object) -> str:
        """Единицы PnL карточки: RUB для сделок со снапшотом факторов, иначе RAW."""
        if price_step in (None, "") or step_cost in (None, ""):
            return "RAW"
        try:
            return "RUB" if Decimal(str(price_step)) != 0 else "RAW"
        except InvalidOperation:
            return "RAW"

    def _display_contract(self, ticker: str) -> str:
        """Короткое имя контракта; без известного имени — «контракт не указан».

        Сырой биржевой тикер пользователю не показывается нигде, включая журнал
        и ``trade_summary.csv`` (правило именования в ``openspec/config.yaml``).
        """
        return self._names.get(ticker) or "контракт не указан"

    @staticmethod
    def _strategy_tf(trade_id: object) -> str:
        parts = str(trade_id or "").split(":")
        if len(parts) >= 2 and parts[1]:
            return f"{parts[0]} ({parts[1]})"
        return parts[0] if parts else ""

    @staticmethod
    def _describe_event(event_type: str, action_type: str) -> str:
        et = (event_type or "").upper()
        if et == "FEE_ADJUSTMENT":
            return "Комиссия уточнена брокером"
        status = _STATUS_LABELS.get(et, event_type or "")
        action, _, target = (action_type or "").partition(":")
        if action == "TARGET" and target:
            action_label = f"цель {target}"
        else:
            action_label = _ACTION_LABELS.get(action)
        if et == "FILL":
            fill_phrases = {
                "OPEN": "Позиция открыта",
                "ADD": "Позиция пополнена",
                "REDUCE": "Часть позиции закрыта",
                "CLOSE": "Позиция закрыта",
            }
            if action in fill_phrases:
                return fill_phrases[action]
            if action == "TARGET" and target:
                return f"Цель {target} исполнена"
            if action == "STOP":
                return "Стоп исполнен"
            return status or "Исполнено"
        if et == "PARTIAL":
            suffix = f": {action_label}" if action_label else ""
            return "Исполнено частично" + suffix
        if action_label:
            return f"{status}: {action_label}"
        return status or et or "Событие"

    @staticmethod
    def _write_temp(
        path: Path,
        columns: tuple[str, ...],
        headers: tuple[str, ...],
        rows: list[dict[str, object]],
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {header: row.get(column, "") for header, column in zip(headers, columns)}
                    )
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _record_success(self, revision: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET exported_revision = ?, failed_revision = NULL, "
                "last_error = NULL, updated_at = datetime('now') WHERE export_id = 1 "
                "AND required_revision <= ?",
                (revision, revision),
            )

    def _record_failure(self, revision: int, error: OSError) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET failed_revision = ?, last_error = ?, updated_at = datetime('now') "
                "WHERE export_id = 1",
                (revision, str(error)),
            )


class AuditExporter:
    """Exports each durable calculation trace once per successful file write."""

    def __init__(self, connection, path: Path, *, max_bytes: int, backup_count: int) -> None:
        self._connection = connection
        self._path = path
        self._handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def export(self) -> bool:
        """Retry unexported traces without changing their SQLite records."""
        revision = self._connection.execute(
            "SELECT required_revision FROM export_state WHERE export_id = 1"
        ).fetchone()[0]
        rows = self._connection.execute(
            """
            SELECT calculation_id, source_data_id, trade_id, command_id, event_id,
                   assignment_id, signal_id, service_uid, correlation_id, algorithm,
                   algorithm_version, input_json, steps_json, rounding_json, output_json,
                   outcome, reason, created_at
            FROM calculations
            WHERE calculation_id NOT IN (SELECT calculation_id FROM audit_exports)
            ORDER BY created_at, calculation_id
            """
        ).fetchall()
        columns = (
            "calculation_id", "source_data_id", "trade_id", "command_id", "event_id",
            "assignment_id", "signal_id", "service_uid", "correlation_id", "algorithm",
            "algorithm_version", "inputs", "steps", "rounding", "result", "outcome",
            "reason", "created_at",
        )
        try:
            for row in rows:
                record = dict(zip(columns, row, strict=True))
                for field in ("inputs", "steps", "rounding", "result"):
                    record[field] = json.loads(record[field])
                self._write(record)
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO audit_exports (calculation_id, exported_at) VALUES (?, datetime('now'))",
                        (record["calculation_id"],),
                    )
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            self._record_failure(revision, error)
            log.error("trade audit export revision %s failed: %s", revision, error)
            return False
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET audit_exported_revision = ?, audit_failed_revision = NULL, "
                "audit_last_error = NULL, updated_at = datetime('now') WHERE export_id = 1",
                (revision,),
            )
        return True

    def close(self) -> None:
        self._handler.close()

    def _write(self, record: dict[str, object]) -> None:
        # Write directly so I/O errors are visible to the durable retry loop.
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        self._handler.acquire()
        try:
            if self._handler.shouldRollover(logging.makeLogRecord({"msg": line, "levelno": logging.INFO})):
                self._handler.doRollover()
            if self._handler.stream is None:
                self._handler.stream = self._handler._open()
            self._handler.stream.write(line)
            self._handler.flush()
        finally:
            self._handler.release()

    def _record_failure(self, revision: int, error: Exception) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE export_state SET audit_failed_revision = ?, audit_last_error = ?, "
                "updated_at = datetime('now') WHERE export_id = 1",
                (revision, str(error)),
            )
