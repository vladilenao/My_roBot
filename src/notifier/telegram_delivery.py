"""Долговечное состояние Telegram: receipts, материалы и ручное восстановление."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 3
FILE_RETENTION = timedelta(days=7)


class _Row(tuple):
    """Tuple-совместимая строка с доступом по имени для старых callers/tests."""

    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj._names = names
        return obj

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)


def _row_factory(cursor, row):
    return _Row(row, [column[0] for column in cursor.description])


def namespace(token, chat_id, path=None):
    identity = token.split(":", 1)[0]
    directory = str(Path(path).resolve().parent) if path is not None else ""
    return hashlib.sha256(f"{identity}\0{chat_id}\0{directory}".encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeliveryRepository:
    """Одно соединение SQLite принадлежит одному worker или CLI-процессу."""

    def __init__(self, path, namespace, *, readonly=False):
        self.path = None if path is None else Path(path)
        if self.path is not None and not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        target = ":memory:" if self.path is None else (f"file:{self.path.resolve()}?mode=ro" if readonly else str(self.path))
        self.connection = sqlite3.connect(target, uri=readonly, timeout=2.0)
        self.connection.row_factory = _row_factory
        self.namespace = namespace
        if not readonly:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=2000")
            self._migrate()

    def _migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, SCHEMA_VERSION}:
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
            CREATE TABLE IF NOT EXISTS telegram_files(
                file_id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, filename TEXT NOT NULL,
                mime_type TEXT NOT NULL, content BLOB NOT NULL, size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                operation_id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, operation_key TEXT NOT NULL,
                trade_id TEXT NOT NULL DEFAULT '', event_key TEXT NOT NULL DEFAULT '', method TEXT NOT NULL,
                data_json TEXT NOT NULL, file_id INTEGER REFERENCES telegram_files(file_id),
                file_sha256 TEXT, status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
                media_expired INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(namespace, operation_key));
            CREATE TABLE IF NOT EXISTS attempt_history(
                attempt_id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, operation_id INTEGER NOT NULL,
                source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, detail TEXT, retry_after TEXT,
                FOREIGN KEY(operation_id) REFERENCES operations(operation_id));
            CREATE INDEX IF NOT EXISTS operations_pending ON operations(namespace,status,created_at);
            CREATE TABLE IF NOT EXISTS operation_navigation(
                operation_id INTEGER PRIMARY KEY REFERENCES operations(operation_id),
                data_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS telegram_files_expiry ON telegram_files(namespace,created_at);
        """)
        with self.connection:
            if version < 2:
                # Старые receipts не имели payload/PNG. Их нельзя угадать и
                # переотправить, но они должны быть видимы в списке recovery.
                self.connection.execute("""
                    INSERT OR IGNORE INTO operations(namespace,operation_key,method,data_json,status,message_id,media_expired,created_at,updated_at)
                    SELECT namespace,operation_key,'legacy','{}',status,message_id,1,?,?
                    FROM attempts
                """, (_utc_now(), _utc_now()))
            self.connection.execute("UPDATE attempts SET status='uncertain' WHERE status='attempting'")
            now = _utc_now()
            self.connection.execute("UPDATE operations SET status='uncertain',updated_at=? WHERE status='attempting'", (now,))
            self.connection.execute("UPDATE attempt_history SET status='uncertain',finished_at=? WHERE status='attempting'", (now,))
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def claim_event(self, trade_id, event_key, sequence, revision):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO trades(namespace,trade_id) VALUES (?,?)", (self.namespace, trade_id))
            previous = self.connection.execute("SELECT sequence,revision FROM trades WHERE namespace=? AND trade_id=?", (self.namespace, trade_id)).fetchone()
            inserted = self.connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (self.namespace, trade_id, event_key)).rowcount
            if not inserted or sequence < previous[0] or revision < previous[1]:
                return False
            self.connection.execute("UPDATE trades SET sequence=?,revision=? WHERE namespace=? AND trade_id=?", (sequence, revision, self.namespace, trade_id))
            return True

    def save_operation(self, key, method, data, photo=None, *, trade_id="", event_key="", navigation=None):
        """Сохраняет точное представление и PNG BLOB до первого HTTP."""
        now = _utc_now()
        with self.connection:
            existing = self.connection.execute("SELECT operation_id FROM operations WHERE namespace=? AND operation_key=?", (self.namespace, key)).fetchone()
            if existing:
                return existing[0]
            file_id = file_hash = None
            if photo is not None:
                file_hash = hashlib.sha256(photo).hexdigest()
                file_id = self.connection.execute(
                    "INSERT INTO telegram_files(namespace,filename,mime_type,content,size_bytes,sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                    (self.namespace, "trade.png", "image/png", photo, len(photo), file_hash, now),
                ).lastrowid
            operation_id = self.connection.execute(
                "INSERT INTO operations(namespace,operation_key,trade_id,event_key,method,data_json,file_id,file_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.namespace, key, trade_id, event_key, method, json.dumps(data, ensure_ascii=False), file_id, file_hash, now, now),
            ).lastrowid
            if navigation is not None:
                self.connection.execute("INSERT INTO operation_navigation VALUES (?,?)",
                                        (operation_id, json.dumps(navigation, ensure_ascii=False)))
            return operation_id

    def trade_operations(self, trade_id):
        """Материалы навигации без загрузки PNG, в том числе после очистки файлов."""
        rows = self.connection.execute("""
            SELECT o.operation_id,o.operation_key,o.event_key,o.method,o.status,o.message_id,
                   o.data_json,n.data_json AS navigation_json
            FROM operations o LEFT JOIN operation_navigation n USING(operation_id)
            WHERE o.namespace=? AND o.trade_id=? ORDER BY o.operation_id
        """, (self.namespace, trade_id)).fetchall()
        return [dict(zip(row._names, row)) for row in rows]

    def supersede_navigation(self, trade_id, message_id, current_id):
        """Успешная актуальная клавиатура заменяет прежние неудачные правки."""
        with self.connection:
            for op in self.trade_operations(trade_id):
                if (op["event_key"] != "navigation" or op["operation_id"] == current_id
                        or op["status"] in {"confirmed", "superseded"}
                        or json.loads(op["data_json"]).get("message_id") != message_id):
                    continue
                self.connection.execute("UPDATE operations SET status='superseded',updated_at=? WHERE operation_id=? AND namespace=?",
                                        (_utc_now(), op["operation_id"], self.namespace))
                self.connection.execute("UPDATE attempts SET status='superseded' WHERE namespace=? AND operation_key=?",
                                        (self.namespace, op["operation_key"]))

    def operation(self, key):
        return self.connection.execute("SELECT * FROM operations WHERE namespace=? AND operation_key=?", (self.namespace, key)).fetchone()

    def operation_material(self, operation_id):
        row = self.connection.execute("SELECT o.*,f.content,f.size_bytes,f.sha256 FROM operations o LEFT JOIN telegram_files f ON f.file_id=o.file_id WHERE o.namespace=? AND o.operation_id=?", (self.namespace, operation_id)).fetchone()
        if row is None:
            return None
        names = ("operation_id", "namespace", "operation_key", "trade_id", "event_key", "method", "data_json",
                 "file_id", "file_sha256", "status", "message_id", "media_expired", "created_at", "updated_at",
                 "content", "size_bytes", "sha256")
        result = dict(zip(names, row, strict=True))
        result["data"] = json.loads(result.pop("data_json"))
        photo = result.get("content")
        if result["file_id"] is not None or result["media_expired"]:
            if result["media_expired"] or photo is None or len(photo) != result["size_bytes"] or hashlib.sha256(photo).hexdigest() != result["file_sha256"]:
                result["media_error"] = "Картинка удалена или повреждена: срок хранения 7 дней."
            else:
                result["photo"] = photo
        return result

    def begin_operation(self, operation_id, *, source="initial"):
        row = self.connection.execute("SELECT status,operation_key FROM operations WHERE namespace=? AND operation_id=?", (self.namespace, operation_id)).fetchone()
        if row is None or row[0] in {"confirmed", "superseded"}:
            return False
        now = _utc_now()
        with self.connection:
            self.connection.execute("UPDATE operations SET status='attempting',updated_at=? WHERE namespace=? AND operation_id=?", (now, self.namespace, operation_id))
            self.connection.execute("INSERT INTO attempt_history(namespace,operation_id,source,started_at,status) VALUES (?,?,?,?,?)", (self.namespace, operation_id, source, now, "attempting"))
            # Legacy receipts remain populated for compatibility and restart deduplication.
            self.connection.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,'attempting',NULL)", (self.namespace, row["operation_key"]))
        return True

    def finish_operation(self, operation_id, response):
        message_id = response.result.get("message_id") if isinstance(response.result, dict) and response.ok else None
        status = "confirmed" if response.ok else "uncertain" if response.uncertain else "failed"
        now = _utc_now()
        with self.connection:
            self.connection.execute("UPDATE operations SET status=?,message_id=?,updated_at=? WHERE namespace=? AND operation_id=?", (status, message_id, now, self.namespace, operation_id))
            key = self.connection.execute("SELECT operation_key FROM operations WHERE namespace=? AND operation_id=?", (self.namespace, operation_id)).fetchone()[0]
            self.connection.execute("UPDATE attempts SET status=?,message_id=? WHERE namespace=? AND operation_key=?", (status, message_id, self.namespace, key))
            self.connection.execute("UPDATE attempt_history SET status=?,finished_at=? WHERE attempt_id=(SELECT max(attempt_id) FROM attempt_history WHERE namespace=? AND operation_id=?)", (status, now, self.namespace, operation_id))
        return message_id

    # Compatibility with the original receipt API.
    def begin_attempt(self, key):
        with self.connection:
            return bool(self.connection.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,'attempting',NULL)", (self.namespace, key)).rowcount)

    def finish_attempt(self, key, response):
        message_id = response.result.get("message_id") if isinstance(response.result, dict) and response.ok else None
        status = "confirmed" if response.ok else "uncertain" if response.uncertain else "failed"
        with self.connection:
            self.connection.execute("UPDATE attempts SET status=?,message_id=? WHERE namespace=? AND operation_key=?", (status, message_id, self.namespace, key))
        return message_id

    def pending(self, *, trade_id=None, date_from=None, date_to=None, include_confirmed=False):
        clauses, args = ["namespace=?"], [self.namespace]
        if not include_confirmed:
            clauses.append("status NOT IN ('confirmed','superseded')")
        if trade_id:
            clauses.append("trade_id=?")
            args.append(trade_id)
        if date_from:
            clauses.append("created_at >= ?")
            args.append(date_from)
        if date_to:
            clauses.append("created_at < ?")
            args.append(date_to)
        return self.connection.execute("SELECT operation_id,operation_key,trade_id,event_key,method,status,media_expired,created_at FROM operations WHERE " + " AND ".join(clauses) + " ORDER BY operation_id", args).fetchall()

    def cleanup_files(self, now=None):
        cutoff = (now or datetime.now(timezone.utc)) - FILE_RETENTION
        with self.connection:
            ids = [r[0] for r in self.connection.execute("SELECT file_id FROM telegram_files WHERE namespace=? AND created_at < ?", (self.namespace, cutoff.isoformat())).fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.connection.execute(f"UPDATE operations SET media_expired=1,file_id=NULL,updated_at=? WHERE namespace=? AND file_id IN ({placeholders})", [_utc_now(), self.namespace, *ids])
                self.connection.execute(f"DELETE FROM telegram_files WHERE file_id IN ({placeholders})", ids)
            return len(ids)

    def root(self, trade_id):
        return self.connection.execute("SELECT root_id,root_kind,root_text FROM trades WHERE namespace=? AND trade_id=? AND root_id IS NOT NULL", (self.namespace, trade_id)).fetchone()

    def set_root(self, trade_id, message_id, kind, text):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO trades(namespace,trade_id) VALUES (?,?)", (self.namespace, trade_id))
            self.connection.execute("UPDATE trades SET root_id=?,root_kind=?,root_text=? WHERE namespace=? AND trade_id=?", (message_id, kind, text, self.namespace, trade_id))

    def add_stage(self, trade_id, role, message_id):
        with self.connection:
            self.connection.execute("INSERT INTO stages VALUES (?,?,?,?) ON CONFLICT(namespace,trade_id,role) DO UPDATE SET message_id=excluded.message_id", (self.namespace, trade_id, role, message_id))

    def stages(self, trade_id):
        return self.connection.execute("SELECT role,message_id FROM stages WHERE namespace=? AND trade_id=? ORDER BY rowid", (self.namespace, trade_id)).fetchall()

    def update_root_text(self, trade_id, text):
        with self.connection:
            self.connection.execute("UPDATE trades SET root_text=? WHERE namespace=? AND trade_id=?", (text, self.namespace, trade_id))

    def close(self):
        self.connection.close()


def markup(buttons):
    return json.dumps({"inline_keyboard": [buttons[i:i+3] for i in range(0, len(buttons), 3)]}, ensure_ascii=False)
