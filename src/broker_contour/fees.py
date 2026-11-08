"""Настоящие комиссии из операций брокера (add-account-vs-emulation-report, решение 6).

Брокер контура отдаёт исполнение без комиссии, и менеджер сделок ставит оценку
из настроек (``fee_source = configured``). На каждом проходе сверщика
``FeeSync`` находит операции покупки и продажи с номерами сделок биржи ждущих
исполнений и выпускает ``FeeAdjustment`` — журнал правит комиссию через
``cost_adjustments`` (поправка уже есть в reducer'е).

Комиссия операции — сумма «комиссий брокера», ссылающихся на неё
(``parent_operation_id``). Нет их, а операции больше 15 минут — поле комиссии
самой операции. Комиссия делится между сделками биржи операции по количеству.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from src.broker.port import FeeAdjustment
from src.broker_contour.api import OperationReport, OrderFailure

log = logging.getLogger(__name__)

EXECUTION_PREFIX = "tinvest:"
HORIZON = timedelta(days=7)
FALLBACK_AFTER = timedelta(minutes=15)
LOOKBACK = timedelta(minutes=1)
MAX_PAGES = 10
_CENT = Decimal("0.01")

_PENDING = (
    "SELECT execution_id, executed_at FROM fills "
    "WHERE COALESCE(fee_source, '') <> 'broker' AND execution_id LIKE ? AND executed_at >= ? "
    "ORDER BY executed_at, execution_id"
)
_STALE = (
    "SELECT COUNT(*) FROM fills "
    "WHERE COALESCE(fee_source, '') <> 'broker' AND execution_id LIKE ? AND executed_at < ?"
)


@dataclass(frozen=True)
class PendingFill:
    execution_id: str
    trade_number: str
    executed_at: datetime


def _when(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


class FeeSync:
    """Сопоставление комиссий операций исполнениям журнала сделок контура."""

    def __init__(self, api, connection: Callable[[], sqlite3.Connection]):
        self._api = api
        self._connection = connection
        self._stale_warned = False

    # --- журнал -------------------------------------------------------------
    def pending(self, now: datetime) -> list[PendingFill]:
        """Исполнения от сделок биржи без комиссии брокера, не старше ``HORIZON``."""
        rows = self._connection().execute(_PENDING, (EXECUTION_PREFIX + "%", (now - HORIZON).isoformat())).fetchall()
        return [PendingFill(row[0], row[0].removeprefix(EXECUTION_PREFIX), _when(row[1])) for row in rows]

    def _warn_stale(self, now: datetime) -> None:
        if self._stale_warned:
            return
        count = self._connection().execute(_STALE, (EXECUTION_PREFIX + "%", (now - HORIZON).isoformat())).fetchone()[0]
        if count:
            self._stale_warned = True
            log.warning("Комиссии брокера не найдены у %d исполнений старше %d дней: остаются с оценкой",
                        count, HORIZON.days)

    # --- проход ---------------------------------------------------------------
    def run(self, now: datetime) -> list[FeeAdjustment]:
        """Поправки комиссий; сбой брокера — пустой список, исполнения ждут следующего прохода."""
        try:
            self._warn_stale(now)
            pending = self.pending(now)
        except sqlite3.Error as exc:
            log.warning("Комиссии из операций: журнал сделок не прочитан (%s)", type(exc).__name__)
            return []
        if not pending:
            return []
        since = min(fill.executed_at for fill in pending) - LOOKBACK
        try:
            operations = self._api.operations(since, now, max_pages=MAX_PAGES)
        except OrderFailure as exc:
            log.warning("Комиссии из операций не прочитаны (%s)", exc.code or exc.kind)
            return []
        return adjustments(pending, operations, now)


def adjustments(pending: list[PendingFill], operations: list[OperationReport], now: datetime) -> list[FeeAdjustment]:
    """Поправки по ждущим исполнениям, у операций которых комиссия уже известна."""
    by_number = {fill.trade_number: fill for fill in pending}
    fees: dict[str, Decimal] = {}
    for operation in operations:
        if operation.kind == "fee" and operation.parent_id:
            fees[operation.parent_id] = fees.get(operation.parent_id, Decimal(0)) + operation.amount
    result: list[FeeAdjustment] = []
    for operation in operations:
        if operation.kind != "trade" or not operation.trades:
            continue
        mine = [number for number, _ in operation.trades if number in by_number]
        if not mine:
            continue
        if operation.operation_id in fees:
            source, total = "broker_fee", fees[operation.operation_id]
        elif operation.commission is not None and operation.at is not None and now - operation.at >= FALLBACK_AFTER:
            source, total = "commission", operation.commission
        else:
            continue
        for number, fee in _split(total, operation.trades).items():
            fill = by_number.get(number)
            if fill is None:
                continue
            result.append(FeeAdjustment(
                adjustment_id=f"opfee:{source}:{operation.operation_id}:{fill.execution_id}",
                execution_id=fill.execution_id, new_fee=fee, timestamp=now))
    return result


def _split(total: Decimal, trades: tuple[tuple[str, int], ...]) -> dict[str, Decimal]:
    """Комиссия операции по сделкам биржи пропорционально количеству, остаток копеек — последней."""
    quantity = sum(max(qty, 0) for _, qty in trades)
    if quantity <= 0:
        share = _cents(total / len(trades))
        parts = {number: share for number, _ in trades}
    else:
        parts = {number: _cents(total * max(qty, 0) / quantity) for number, qty in trades}
    last = trades[-1][0]
    parts[last] = max(total - sum(value for number, value in parts.items() if number != last), Decimal(0))
    return parts
