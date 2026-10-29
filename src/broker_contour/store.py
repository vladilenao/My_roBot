"""``broker.sqlite3`` контура на брокере: отпечаток счёта и отчёты сверщика.

Пишет только процесс контура; API ядра читает базу только на чтение
(``read_last_report``). Полный номер счёта в базу не попадает.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BROKER_DATABASE_FILE = "broker.sqlite3"
REPORTS_KEPT = 100


@dataclass(frozen=True)
class Fingerprint:
    broker: str
    account_hash: str
    account_tail: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class BrokerStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    broker TEXT NOT NULL,
                    account_hash TEXT NOT NULL,
                    account_tail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    result TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def fingerprint(self) -> Fingerprint | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT broker, account_hash, account_tail FROM account WHERE id=1").fetchone()
        return None if row is None else Fingerprint(row["broker"], row["account_hash"], row["account_tail"])

    def save_fingerprint(self, fingerprint: Fingerprint) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO account(id, broker, account_hash, account_tail, created_at) VALUES(1,?,?,?,?)",
                         (fingerprint.broker, fingerprint.account_hash, fingerprint.account_tail, _now()))

    def add_report(self, report: dict) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO reconciliations(at, result, report_json) VALUES(?,?,?)",
                         (report["at"], report["result"], json.dumps(report, ensure_ascii=False)))
            conn.execute("DELETE FROM reconciliations WHERE id NOT IN "
                         "(SELECT id FROM reconciliations ORDER BY id DESC LIMIT ?)", (REPORTS_KEPT,))


def read_last_report(path: Path) -> dict | None:
    """Сторона API: последний отчёт сверщика только на чтение; базы ещё нет — ``None``."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        try:
            row = conn.execute("SELECT report_json FROM reconciliations ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return None
    return None if row is None else json.loads(row[0])
