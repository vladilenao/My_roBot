"""Журнал заявок брокера в ``broker.sqlite3`` (add-tinvest-orders, design.md решение 4).

Одна строка ``broker_orders`` — одна заявка у брокера; пишется до ``PostOrder``.
Одна строка ``broker_executions`` — одна сделка биржи; пишется до ``consume`` и
помечается проведённой после. Outbox ``TradeManager`` отвечает за доставку
команды в ``submit``, этот журнал — за жизнь заявки у брокера. Номер счёта
здесь не хранится. Пишут главный поток и поток стрима — одно соединение под замком.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Постоянное пространство имён ключей: тот же ``command_id`` даёт тот же ключ и после рестарта.
KEY_NAMESPACE = uuid.UUID("6f1d3c0e-5a8b-4c2e-9d7f-3b1a2c4d5e6f")

SENDING, ACCEPTED, PARTIAL, FILLED = "sending", "accepted", "partial", "filled"
REJECTED, CANCELLING, CANCELLED, UNKNOWN = "rejected", "cancelling", "cancelled", "unknown"
TERMINAL = frozenset({FILLED, REJECTED, CANCELLED})
IN_FLIGHT = frozenset({SENDING, UNKNOWN, CANCELLING})
KINDS = ("entry", "add", "target", "stop", "exit", "emergency")


def order_key(command_id: str) -> str:
    """Ключ идемпотентности заявки: UUID5 от ``command_id`` (API требует UID до 36 символов)."""
    return str(uuid.uuid5(KEY_NAMESPACE, command_id))


def _text(value) -> str | None:
    return None if value is None else format(value, "f") if isinstance(value, Decimal) else str(value)


@dataclass(frozen=True)
class LedgerOrder:
    key: str
    command_id: str
    trade_id: str
    kind: str
    instrument_uid: str
    side: str
    order_type: str                 # limit · market
    lots: int
    price: Decimal | None = None    # пункты
    state: str = SENDING
    broker_order_id: str | None = None
    lots_executed: int = 0
    average: Decimal | None = None  # средняя исполнения, пункты
    reason: str = ""
    sends: int = 1
    job: str = ""                   # выход, к которому относится заявка (команда или условие стопа)
    target_id: str = ""
    reported: int = 0               # итог (отмена, отказ) передан в TradeManager
    cancel_requested: int = 0       # отмену просил контур, а не брокер (конец дня)
    created_at: str = ""
    sent_at: str = ""
    updated_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def live(self) -> bool:
        return self.state not in TERMINAL

    @property
    def remaining(self) -> int:
        return max(0, self.lots - self.lots_executed)


@dataclass(frozen=True)
class LedgerExecution:
    execution_id: str               # tinvest:<номер сделки биржи>
    key: str
    lots: int
    price: Decimal                  # пункты
    at: datetime
    consumed: int = 0


_COLUMNS = tuple(field.name for field in fields(LedgerOrder))


class OrderLedger:
    def __init__(self, path: Path, *, clock=lambda: datetime.now(timezone.utc)) -> None:
        self.path = Path(path)
        # Часы брокера: время отправки и изменения сравнивается с его «сейчас» (перевыставление, TTL).
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS broker_orders (
                    key TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    instrument_uid TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    price TEXT,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    lots_executed INTEGER NOT NULL DEFAULT 0,
                    average TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    sends INTEGER NOT NULL DEFAULT 1,
                    job TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    reported INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS broker_orders_broker_id ON broker_orders(broker_order_id);
                CREATE INDEX IF NOT EXISTS broker_orders_trade ON broker_orders(trade_id, state);
                CREATE TABLE IF NOT EXISTS broker_executions (
                    execution_id TEXT PRIMARY KEY,
                    key TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    seq INTEGER NOT NULL
                );
            """)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _order(row) -> LedgerOrder:
        data = dict(row)
        for name in ("price", "average"):
            data[name] = None if data[name] is None else Decimal(data[name])
        return LedgerOrder(**data)

    # --- заявки -----------------------------------------------------------
    def create(self, order: LedgerOrder) -> LedgerOrder:
        now = self.clock().isoformat()
        order = replace(order, created_at=order.created_at or now, sent_at=now, updated_at=now)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT INTO broker_orders({','.join(_COLUMNS)}) VALUES({','.join('?' * len(_COLUMNS))})",
                               tuple(_text(getattr(order, name)) if name in ("price", "average")
                                     else getattr(order, name) for name in _COLUMNS))
        return order

    def update(self, key: str, **values) -> LedgerOrder:
        values["updated_at"] = self.clock().isoformat()
        assignments = ", ".join(f"{name}=?" for name in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE broker_orders SET {assignments} WHERE key=?",
                               (*(_text(value) if name in ("price", "average") else value
                                  for name, value in values.items()), key))
        return self.get(key)

    def get(self, key: str) -> LedgerOrder | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM broker_orders WHERE key=?", (key,)).fetchone()
        return None if row is None else self._order(row)

    def by_broker_id(self, broker_order_id: str) -> LedgerOrder | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM broker_orders WHERE broker_order_id=?",
                                     (broker_order_id,)).fetchone()
        return None if row is None else self._order(row)

    def orders(self, *, trade_id: str | None = None, live: bool | None = None, kinds=None) -> list[LedgerOrder]:
        query, params = "SELECT * FROM broker_orders WHERE 1=1", []
        if trade_id is not None:
            query += " AND trade_id=?"
            params.append(trade_id)
        if live is not None:
            query += f" AND state {'NOT ' if live else ''}IN ({','.join('?' * len(TERMINAL))})"
            params.extend(sorted(TERMINAL))
        if kinds:
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY created_at, rowid", params).fetchall()
        return [self._order(row) for row in rows]

    def unreported(self) -> list[LedgerOrder]:
        """Завершённые без исполнения входы, итог которых ещё не передан в ``TradeManager``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM broker_orders WHERE reported=0 AND state IN (?,?) ORDER BY updated_at, rowid",
                (REJECTED, CANCELLED)).fetchall()
        return [self._order(row) for row in rows]

    # --- сделки биржи -----------------------------------------------------
    def add_execution(self, execution: LedgerExecution) -> bool:
        """Новая сделка биржи — ``True``; уже записанная (стрим, затем ``stages``) — ``False``."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM broker_executions WHERE execution_id=?",
                                  (execution.execution_id,)).fetchone():
                return False
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM broker_executions").fetchone()[0]
            self._conn.execute(
                "INSERT INTO broker_executions(execution_id,key,lots,price,at,consumed,seq) VALUES(?,?,?,?,?,0,?)",
                (execution.execution_id, execution.key, execution.lots, _text(execution.price),
                 execution.at.isoformat(), seq))
        return True

    def executions(self, key: str) -> list[LedgerExecution]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM broker_executions WHERE key=? ORDER BY seq", (key,)).fetchall()
        return [self._execution(row) for row in rows]

    def pending_executions(self) -> list[LedgerExecution]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM broker_executions WHERE consumed=0 ORDER BY seq").fetchall()
        return [self._execution(row) for row in rows]

    def mark_consumed(self, execution_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE broker_executions SET consumed=1 WHERE execution_id=?", (execution_id,))

    @staticmethod
    def _execution(row) -> LedgerExecution:
        return LedgerExecution(row["execution_id"], row["key"], row["lots"], Decimal(row["price"]),
                               datetime.fromisoformat(row["at"]), row["consumed"])

    # --- сводка -----------------------------------------------------------
    def in_flight_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM broker_orders WHERE state NOT IN ({','.join('?' * len(TERMINAL))})",
                sorted(TERMINAL)).fetchone()[0]

    def in_flight_instruments(self) -> set[str]:
        """Инструменты с заявкой «в пути» или непроведённым исполнением (сверщик их не сравнивает)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT instrument_uid FROM broker_orders WHERE state IN ({','.join('?' * len(IN_FLIGHT))}) "
                "UNION SELECT o.instrument_uid FROM broker_executions e JOIN broker_orders o USING(key) "
                "WHERE e.consumed=0", sorted(IN_FLIGHT)).fetchall()
        return {row[0] for row in rows}
