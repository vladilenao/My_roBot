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


log = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

JOURNAL_COLUMNS = (
    "occurred_at", "contract", "side", "strategy_tf", "event",
    "quantity", "price", "fee", "reason",
)
POSITIONS_COLUMNS = (
    "contract", "side", "phase", "created_at", "quantity", "average_price", "stop",
    "exit_price", "exit_at", "exit_reason", "net_realized_pnl", "fees", "risk_rub",
    "go_buy", "go_sell", "updated_at",
)

# Человекочитаемые русскоязычные заголовки CSV-проекций. Позиции соответствуют
# внутренним именам в *_COLUMNS (шаг цены/стоимость шага/ГО берутся из метаданных
# контракта брокера, а не из SQLite).
JOURNAL_HEADERS = (
    "Время (МСК)",
    "Контракт",
    "Сторона",
    "Стратегия",
    "Событие",
    "Кол-во",
    "Цена",
    "Комиссия, ₽",
    "Причина",
)
POSITIONS_HEADERS = (
    "Контракт",
    "Сторона",
    "Статус",
    "Открыта",
    "Кол-во, контракты",
    "Цена входа",
    "Стоп",
    "Цена выхода",
    "Выход (МСК)",
    "Причина выхода",
    "Прибыль чистая, ₽",
    "Комиссия, ₽",
    "Риск, ₽",
    "ГО (покупка), ₽",
    "ГО (продажа), ₽",
    "Обновлено (МСК)",
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
            self._backup_legacy_files()
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

    def _backup_legacy_files(self) -> None:
        """Preserve pre-SQLite CSVs once, before their first replacement."""
        for path, headers in (
            (self._journal_path, JOURNAL_HEADERS),
            (self._positions_path, POSITIONS_HEADERS),
        ):
            if path.exists() and not self._is_current_projection(path, headers):
                self._backup_legacy_file(path)

    @staticmethod
    def _is_current_projection(path: Path, headers: tuple[str, ...]) -> bool:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                return tuple(csv.DictReader(handle).fieldnames or ()) == headers
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def _backup_legacy_file(path: Path) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = 0
        while True:
            disambiguator = "" if suffix == 0 else f".{suffix}"
            backup = path.with_name(f"{path.name}.legacy.{timestamp}{disambiguator}")
            try:
                with path.open("rb") as source, backup.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
                return
            except FileExistsError:
                suffix += 1

    def _snapshot(self) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
        self._connection.execute("BEGIN")
        try:
            revision = self._connection.execute(
                "SELECT required_revision FROM export_state WHERE export_id = 1"
            ).fetchone()[0]
            journal_rows = []
            for row in self._rows(
                """
                SELECT events.occurred_at, events.event_type, events.payload_json,
                       trades.trade_id, trades.instrument_id, trades.side,
                       orders.action_type, orders.quantity AS order_quantity,
                       orders.requested_price AS order_price
                FROM events
                LEFT JOIN trades ON trades.trade_id = events.trade_id
                LEFT JOIN orders ON orders.order_id = events.order_id
                ORDER BY event_seq
                """
            ):
                payload = self._payload(row.get("payload_json"))
                quantity = payload.get("quantity") or row.get("order_quantity") or ""
                journal_rows.append({
                    "occurred_at": self._msk(row.get("occurred_at")),
                    "contract": self._display_contract(str(row.get("instrument_id") or "")),
                    "side": _SIDE_LABELS.get(str(row.get("side") or "").upper(), ""),
                    "strategy_tf": self._strategy_tf(row.get("trade_id")),
                    "event": self._describe_event(
                        str(row.get("event_type") or ""), str(row.get("action_type") or "")
                    ),
                    "quantity": quantity,
                    "price": payload.get("price") or row.get("order_price") or "",
                    "fee": self._money(payload.get("fee")),
                    "reason": payload.get("reason") or "",
                })
            position_rows = []
            closing = self._closing_fills()
            reasons = self._exit_reasons()
            risk = self._entry_risk()
            for row in self._rows(
                """
                SELECT trades.instrument_id, positions.trade_id, positions.side, trades.created_at, trades.phase,
                       positions.quantity, positions.average_price, positions.net_realized_pnl,
                       positions.fees, positions.updated_at, protection.stop
                FROM positions
                JOIN trades ON trades.trade_id = positions.trade_id
                LEFT JOIN (
                    SELECT trade_id, COALESCE(confirmed_stop, pending_stop) AS stop
                    FROM protection
                ) protection ON protection.trade_id = positions.trade_id
                ORDER BY positions.trade_id
                """
            ):
                ticker = str(row.get("instrument_id") or "")
                trade_id = str(row.get("trade_id") or "")
                meta = self._contracts.get(ticker)
                close = closing.get(trade_id, {})
                position_rows.append({
                    "contract": self._display_contract(ticker),
                    "side": _SIDE_LABELS.get(str(row.get("side") or "").upper(), ""),
                    "phase": _PHASE_LABELS.get(str(row.get("phase") or "").upper(), str(row.get("phase") or "")),
                    "created_at": self._msk(row.get("created_at")),
                    "quantity": row.get("quantity", ""),
                    "average_price": row.get("average_price") or "",
                    "stop": self._money(row.get("stop")),
                    "exit_price": self._money(close.get("exit_price")) if close else "",
                    "exit_at": self._msk(close.get("exit_at")) if close else "",
                    "exit_reason": reasons.get(trade_id, ""),
                    "net_realized_pnl": self._money(row.get("net_realized_pnl")),
                    "fees": self._money(row.get("fees")),
                    "risk_rub": self._money(risk.get(trade_id)),
                    "go_buy": self._money(str(meta.go_buy)) if meta is not None else "",
                    "go_sell": self._money(str(meta.go_sell)) if meta is not None else "",
                    "updated_at": self._msk(row.get("updated_at")),
                })
        finally:
            self._connection.rollback()
        return revision, journal_rows, position_rows

    def _rows(self, query: str) -> list[dict[str, object]]:
        cursor = self._connection.execute(query)
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

    def _display_contract(self, ticker: str) -> str:
        return self._names.get(ticker) or ticker

    @staticmethod
    def _strategy_tf(trade_id: object) -> str:
        parts = str(trade_id or "").split(":")
        if len(parts) >= 2 and parts[1]:
            return f"{parts[0]} ({parts[1]})"
        return parts[0] if parts else ""

    @staticmethod
    def _describe_event(event_type: str, action_type: str) -> str:
        et = (event_type or "").upper()
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