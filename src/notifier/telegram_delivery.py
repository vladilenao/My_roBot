"""Отдельные durable receipts; connection принадлежит фоновому worker."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def namespace(token, chat_id, path=None):
    # Реальный Bot API token начинается с устойчивого bot_id; ротация секрета
    # того же бота не должна создавать вторую карту карточек.
    identity = token.split(":", 1)[0]
    directory = str(Path(path).resolve().parent) if path is not None else ""
    return hashlib.sha256(f"{identity}\0{chat_id}\0{directory}".encode()).hexdigest()


class DeliveryRepository:
    def __init__(self, path, namespace):
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path is not None else ":memory:")
        self.namespace = namespace
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.connection.close()
            raise ValueError("Неподдерживаемая версия delivery-состояния")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS events(namespace TEXT, trade_id TEXT, event_key TEXT,
                PRIMARY KEY(namespace,trade_id,event_key));
            CREATE TABLE IF NOT EXISTS trades(namespace TEXT, trade_id TEXT, sequence INTEGER NOT NULL DEFAULT -1,
                revision INTEGER NOT NULL DEFAULT -1, root_id INTEGER, root_kind TEXT, root_text TEXT,
                PRIMARY KEY(namespace,trade_id));
            CREATE TABLE IF NOT EXISTS attempts(namespace TEXT, operation_key TEXT, status TEXT NOT NULL,
                message_id INTEGER, PRIMARY KEY(namespace,operation_key));
            CREATE TABLE IF NOT EXISTS stages(namespace TEXT, trade_id TEXT, role TEXT, message_id INTEGER NOT NULL,
                PRIMARY KEY(namespace,trade_id,role));
            PRAGMA user_version=1;
        """)
        # A crash between attempt and response is not proof of delivery/failure.
        with self.connection:
            self.connection.execute("UPDATE attempts SET status='uncertain' WHERE status='attempting'")

    def claim_event(self, trade_id, event_key, sequence, revision):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO trades(namespace,trade_id) VALUES (?,?)", (self.namespace, trade_id))
            previous = self.connection.execute("SELECT sequence,revision FROM trades WHERE namespace=? AND trade_id=?",
                                               (self.namespace, trade_id)).fetchone()
            inserted = self.connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)",
                                                (self.namespace, trade_id, event_key)).rowcount
            if not inserted or sequence < previous[0] or revision < previous[1]:
                return False
            self.connection.execute("UPDATE trades SET sequence=?,revision=? WHERE namespace=? AND trade_id=?",
                                    (sequence, revision, self.namespace, trade_id))
            return True

    def begin_attempt(self, key):
        with self.connection:
            return bool(self.connection.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,'attempting',NULL)",
                                                 (self.namespace, key)).rowcount)

    def finish_attempt(self, key, response):
        message_id = response.result.get("message_id") if isinstance(response.result, dict) and response.ok else None
        with self.connection:
            self.connection.execute("UPDATE attempts SET status=?,message_id=? WHERE namespace=? AND operation_key=?",
                                    ("confirmed" if response.ok else "uncertain" if response.uncertain else "failed",
                                     message_id, self.namespace, key))
        return message_id

    def root(self, trade_id):
        return self.connection.execute("SELECT root_id,root_kind,root_text FROM trades WHERE namespace=? AND trade_id=? AND root_id IS NOT NULL",
                                       (self.namespace, trade_id)).fetchone()

    def set_root(self, trade_id, message_id, kind, text):
        with self.connection:
            self.connection.execute("UPDATE trades SET root_id=?,root_kind=?,root_text=? WHERE namespace=? AND trade_id=?",
                                    (message_id, kind, text, self.namespace, trade_id))

    def add_stage(self, trade_id, role, message_id):
        with self.connection:
            self.connection.execute("INSERT INTO stages VALUES (?,?,?,?) ON CONFLICT(namespace,trade_id,role) "
                                    "DO UPDATE SET message_id=excluded.message_id",
                                    (self.namespace, trade_id, role, message_id))

    def stages(self, trade_id):
        return self.connection.execute("SELECT role,message_id FROM stages WHERE namespace=? AND trade_id=? ORDER BY rowid",
                                       (self.namespace, trade_id)).fetchall()

    def update_root_text(self, trade_id, text):
        with self.connection:
            self.connection.execute("UPDATE trades SET root_text=? WHERE namespace=? AND trade_id=?", (text, self.namespace, trade_id))

    def close(self):
        self.connection.close()


def markup(buttons):
    return json.dumps({"inline_keyboard": [buttons[i:i+3] for i in range(0, len(buttons), 3)]}, ensure_ascii=False)
