"""Разведка комиссий на чужой сделке (add-account-contour, design.md решение 8).

Своих сделок у «Счёта» до Р2 нет, поэтому П25–П26 для настоящего счёта
отвечают на ручной сделке владельца: сверщик увидел, что чужая позиция по
инструменту изменилась, — через 15 минут контур читает операции по нему и
записывает, есть ли в операции номера сделок биржи (П25) и связана ли с ней
«комиссия брокера» (П26). Правило комиссии исполнений не меняется.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from t_tech.invest.schemas import GetOperationsByCursorRequest, OperationState, OperationType
from t_tech.invest.utils import money_to_decimal

from src.logging_setup import get_logger
from src.sandbox.gateway import BrokerCallError
from src.sandbox.records import mask_account

log = get_logger(__name__)

DELAY = timedelta(minutes=15)
ATTEMPTS = 3
KEPT = 10
TRADE_TYPES = (OperationType.OPERATION_TYPE_BUY, OperationType.OPERATION_TYPE_SELL)


@dataclass
class _Pending:
    uid: str
    name: str
    since: datetime
    due: datetime
    attempts: int = 0


def _money(value) -> str | None:
    if value is None or not getattr(value, "currency", None):
        return None
    return format(abs(money_to_decimal(value)).normalize(), "f")


def _trades(item) -> list:
    trades = getattr(getattr(item, "trades_info", None), "trades", None)
    return trades if isinstance(trades, list) else []


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class FeeProbes:
    """Очередь разведок по инструментам с изменившейся чужой позицией и их ответы в ``broker.sqlite3``."""

    def __init__(self, path: Path, *, delay: timedelta = DELAY, attempts: int = ATTEMPTS) -> None:
        self.path = Path(path)
        self._delay = delay
        self._attempts = attempts
        self._holdings: dict[str, int] | None = None
        self._uids: dict[str, str] = {}      # короткое имя → uid по всем снимкам: закрытая позиция уже без имени
        self._last_at: datetime | None = None
        self._pending: dict[str, _Pending] = {}
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS fee_probes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "at TEXT NOT NULL, probe_json TEXT NOT NULL)")

    def observe(self, now: datetime, snapshot) -> None:
        """Снимок сверки: изменилась позиция по инструменту — разведка через ``delay``; первый снимок — база."""
        holdings = dict(snapshot.holdings)
        self._uids.update({name: uid for uid, name in (snapshot.names or {}).items()})
        if self._holdings is not None:
            for name in sorted(set(holdings) | set(self._holdings)):
                if holdings.get(name, 0) != self._holdings.get(name, 0) and name in self._uids:
                    since = (self._last_at or now) - timedelta(minutes=1)
                    self._pending[name] = _Pending(self._uids[name], name, since, now + self._delay)
        self._holdings, self._last_at = holdings, now

    def run(self, gateway, account_id: str, now: datetime) -> None:
        """Разведки, срок которых пришёл; сбой чтения — тип ошибки, повтор на следующей сверке до ``attempts``."""
        for name, item in list(self._pending.items()):
            if now < item.due:
                continue
            try:
                response = gateway.call("operations.get_operations_by_cursor", GetOperationsByCursorRequest(
                    account_id=account_id, instrument_id=item.uid, from_=item.since, to=now))
            except BrokerCallError as exc:
                item.attempts += 1
                log.warning("Разведка комиссий %s: операции не прочитаны (%s), попытка %d из %d",
                            name, exc.code, item.attempts, self._attempts)
                if item.attempts >= self._attempts:
                    self._save(now, {"instrument": name, "operation": None, "trades_info": False,
                                     "broker_fee_linked": False, "broker_fee": None, "commission": None,
                                     "error": exc.code})
                    del self._pending[name]
                continue
            del self._pending[name]
            items = [op for op in response.items if op.state == OperationState.OPERATION_STATE_EXECUTED]
            for trade in [op for op in items if op.type in TRADE_TYPES]:
                fee = next((op for op in items if op.type == OperationType.OPERATION_TYPE_BROKER_FEE
                            and op.parent_operation_id and op.parent_operation_id == trade.id), None)
                self._save(now, {"instrument": name, "operation": mask_account(trade.id),
                                 "trades_info": bool([t for t in _trades(trade) if t.num]),
                                 "broker_fee_linked": fee is not None,
                                 "broker_fee": None if fee is None else _money(fee.payment),
                                 "commission": _money(getattr(trade, "commission", None)), "error": None})

    def _save(self, now: datetime, probe: dict) -> None:
        probe = {"at": _stamp(now), **probe}
        log.info("Разведка комиссий %s: операция %s, П25 номера сделок %s, П26 комиссия брокера связана %s "
                 "(%s ₽), поле commission %s ₽%s", probe["instrument"], probe["operation"] or "—",
                 "да" if probe["trades_info"] else "нет", "да" if probe["broker_fee_linked"] else "нет",
                 probe["broker_fee"] or "—", probe["commission"] or "—",
                 f", ошибка {probe['error']}" if probe["error"] else "")
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("INSERT INTO fee_probes(at, probe_json) VALUES(?,?)",
                         (probe["at"], json.dumps(probe, ensure_ascii=False)))
            conn.execute("DELETE FROM fee_probes WHERE id NOT IN (SELECT id FROM fee_probes ORDER BY id DESC LIMIT ?)",
                         (KEPT,))


def read_fee_probes(path: Path) -> list[dict]:
    """Сторона API: последние разведки, новые сверху; базы или таблицы нет — пусто."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        try:
            rows = conn.execute("SELECT probe_json FROM fee_probes ORDER BY id DESC LIMIT ?", (KEPT,)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [json.loads(row[0]) for row in rows]
