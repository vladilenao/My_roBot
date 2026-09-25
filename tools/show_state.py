"""
Консольный отчёт о живом состоянии журнала сделок.

Открывает базу строго на чтение и печатает один согласованный снимок в четырёх
блоках: счёт с загрузкой лимитов, сделки в работе, активные резервы и последние
отказы биржи. Инструмент ничего не чинит и ничего не пишет: освобождение
резерва остаётся обязанностью компонента-писателя состояния.

Пример:
    python tools/show_state.py
    python tools/show_state.py --database data/trades.sqlite3
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MSK = timezone(timedelta(hours=3))
TERMINAL_PHASES = ("CLOSED", "CANCELLED", "REJECTED", "ERROR")
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "trades.sqlite3"


class StateUnavailable(RuntimeError):
    """База недоступна для чтения или слишком старая для отчёта."""


@dataclass(frozen=True)
class Limits:
    per_trade_pct: Decimal
    per_instrument_pct: Decimal
    portfolio_pct: Decimal


def load_limits() -> Limits:
    """Прочитать лимиты из конфигурации робота, а не из констант отчёта."""
    from src.config import RISK_LIMITS

    return Limits(
        per_trade_pct=Decimal(str(RISK_LIMITS["trade_pct"])),
        per_instrument_pct=Decimal(str(RISK_LIMITS["instrument_pct"])),
        portfolio_pct=Decimal(str(RISK_LIMITS["portfolio_pct"])),
    )


def parse_timestamp(text: str | None) -> datetime | None:
    """Привести запись времени базы к московскому времени.

    В базе смешаны три формата: время бара без смещения (уже МСК), ISO со
    смещением (UTC) и результат ``datetime('now')`` (UTC с пробелом вместо
    ``T``). Нераспознанный формат возвращает ``None``, а не угадывание.
    """
    if not text:
        return None
    raw = text.strip()
    try:
        if "T" not in raw and " " in raw:
            moment = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def format_moment(moment: datetime | None) -> str:
    return moment.strftime("%d.%m.%Y %H:%M") if moment else "—"


def format_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "—"
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{seconds // 86400} д {(seconds % 86400) // 3600} ч"


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def format_percent_value(value: Decimal) -> str:
    return f"{value:f}".replace(".", ",") + "%"


def format_price(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return f"{Decimal(value).normalize():f}"
    except ArithmeticError:
        return value


def percent(part: Decimal, whole: Decimal) -> str:
    if whole <= 0:
        return "—"
    return f"{part / whole * Decimal(100):.1f}".replace(".", ",") + "%"


def render_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> list[str]:
    """Собрать таблицу фиксированной ширины по самым длинным значениям."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    if not rows:
        lines.append("нет данных")
    return lines


def open_readonly(database: Path) -> sqlite3.Connection:
    """Открыть базу только на чтение, не создавая и не мигрируя схему."""
    if not database.exists():
        raise StateUnavailable(f"база не найдена: {database}")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.execute("SELECT 1").fetchone()
    except sqlite3.Error as error:
        raise StateUnavailable(
            f"не удалось прочитать базу только на чтение: {error}\n"
            "Обычно это означает, что робот остановлен и удалил служебный файл -shm.\n"
            "Запустите робота и повторите либо откройте копию базы."
        ) from error
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 9:
        connection.close()
        raise StateUnavailable(
            f"схема базы версии {version}, отчёту нужна версия 9.\n"
            "Запустите робот один раз на обновлённом коде — он обновит базу и запишет имена контрактов."
        )
    return connection


def read_names(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT ticker, short_name FROM instrument_names").fetchall()
    return {ticker: short_name for ticker, short_name in rows}


def contract_label(ticker: str, names: dict[str, str]) -> str:
    """Показать короткое имя; сырой тикер пользователю не выводится."""
    return names.get(ticker, "имя не записано")


def collect_account(connection: sqlite3.Connection) -> tuple[Decimal, Decimal]:
    row = connection.execute("SELECT balance, equity FROM account WHERE account_id = 1").fetchone()
    if row is None:
        return Decimal("0"), Decimal("0")
    return Decimal(row[0] or "0"), Decimal(row[1] or "0")


def collect_reservation_totals(connection: sqlite3.Connection) -> tuple[Decimal, Decimal]:
    row = connection.execute(
        "SELECT COALESCE(SUM(risk_amount), '0'), COALESCE(SUM(margin_amount), '0') "
        "FROM reservations WHERE status = 'ACTIVE'"
    ).fetchone()
    return Decimal(row[0]), Decimal(row[1])


def collect_open_trades(
    connection: sqlite3.Connection, names: dict[str, str]
) -> tuple[tuple[str, ...], ...]:
    placeholders = ", ".join("?" * len(TERMINAL_PHASES))
    rows = connection.execute(
        f"""
        SELECT t.instrument_id, t.side, t.phase, p.quantity, p.average_price,
               pr.confirmed_stop, pr.pending_stop, t.price_step, t.step_cost
        FROM trades t
        LEFT JOIN positions p ON p.trade_id = t.trade_id
        LEFT JOIN protection pr ON pr.trade_id = t.trade_id
        WHERE t.phase NOT IN ({placeholders})
        ORDER BY t.created_at
        """,
        TERMINAL_PHASES,
    ).fetchall()
    result = []
    for instrument, side, phase, quantity, average, confirmed, pending, step, cost in rows:
        stop = confirmed or pending
        risk = None
        if average and stop and step and cost:
            step_value, cost_value = Decimal(step), Decimal(cost)
            if step_value > 0 and cost_value > 0:
                risk = abs(Decimal(average) - Decimal(stop)) / step_value * cost_value * Decimal(quantity or 0)
        result.append(
            (
                contract_label(instrument, names),
                "ПОКУПКА" if side == "BUY" else "ПРОДАЖА",
                phase,
                str(quantity or 0),
                format_price(average),
                format_price(stop) + ("" if confirmed else " (ожидается)"),
                format_money(risk),
            )
        )
    return tuple(result)


def collect_reservations(
    connection: sqlite3.Connection, names: dict[str, str], now: datetime
) -> tuple[tuple[str, ...], ...]:
    rows = connection.execute(
        """
        SELECT r.trade_id, t.instrument_id, t.phase, r.risk_amount, r.margin_amount, r.created_at
        FROM reservations r
        LEFT JOIN trades t ON t.trade_id = r.trade_id
        WHERE r.status = 'ACTIVE'
        ORDER BY r.created_at
        """
    ).fetchall()
    result = []
    for _trade_id, instrument, phase, risk, margin, created_at in rows:
        created = parse_timestamp(created_at)
        orphaned = phase in TERMINAL_PHASES
        result.append(
            (
                contract_label(instrument, names),
                format_money(Decimal(risk)),
                format_money(Decimal(margin)),
                format_moment(created),
                format_age(created, now),
                "СИРОТА: сделка " + phase if orphaned else "",
            )
        )
    return tuple(result)


def collect_broker_rejections(
    connection: sqlite3.Connection, names: dict[str, str], limit: int
) -> tuple[tuple[str, ...], ...]:
    rows = connection.execute(
        """
        SELECT e.occurred_at, e.event_type, t.instrument_id, e.payload_json
        FROM events e
        LEFT JOIN trades t ON t.trade_id = e.trade_id
        WHERE e.event_type IN ('REJECT', 'CANCEL')
        ORDER BY e.rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for occurred_at, event_type, instrument, payload in rows:
        reason = ""
        try:
            reason = str(json.loads(payload or "{}").get("reason") or "причина не указана")
        except json.JSONDecodeError:
            reason = "причина не читается"
        result.append(
            (
                format_moment(parse_timestamp(occurred_at)),
                "отклонена" if event_type == "REJECT" else "отменена",
                contract_label(instrument, names),
                reason,
            )
        )
    return tuple(result)


def build_report(
    connection: sqlite3.Connection,
    names: dict[str, str],
    limits: Limits,
    now: datetime,
    rejection_limit: int,
) -> str:
    balance, equity = collect_account(connection)
    budget_base = min(balance, equity)
    used_risk, used_margin = collect_reservation_totals(connection)
    risk_budget = budget_base * limits.per_trade_pct / Decimal(100)
    open_trades = collect_open_trades(connection, names)
    reservations = collect_reservations(connection, names, now)
    rejections = collect_broker_rejections(connection, names, rejection_limit)

    lines: list[str] = []
    lines.append(f"Состояние журнала сделов · снимок {format_moment(now)} МСК")
    if not names:
        lines.append(
            "Имена контрактов не записаны: запустите робот на обновлённом коде, "
            "чтобы карта коротких имён попала в базу."
        )
    lines.append("")

    lines.append("СЧЁТ И ЛИМИТЫ")
    lines.append(f"  Баланс {format_money(balance)} ₽ · Эквити {format_money(equity)} ₽")
    lines.append(f"  База бюджета (минимум баланса и эквити): {format_money(budget_base)} ₽")
    lines.append(
        f"  Риск на сделку: {format_money(used_risk)} / {format_money(risk_budget)} ₽ "
        f"({percent(used_risk, risk_budget)}) — лимит {format_percent_value(limits.per_trade_pct)}"
    )
    lines.append(
        f"  Гарантийное обеспечение: {format_money(used_margin)} / {format_money(budget_base)} ₽ "
        f"({percent(used_margin, budget_base)})"
    )
    lines.append(
        f"  Лимиты конфигурации: на инструмент {format_percent_value(limits.per_instrument_pct)}, "
        f"по портфелю {format_percent_value(limits.portfolio_pct)}"
    )
    lines.append("  Занятость посчитана по всем резервам ACTIVE, как считает резервирование.")
    lines.append("")

    lines.append("СДЕЛКИ В РАБОТЕ")
    lines.extend(
        "  " + line
        for line in render_table(
            ("Контракт", "Сторона", "Фаза", "Объём", "Средняя", "Стоп", "Риск, ₽"),
            open_trades,
        )
    )
    lines.append("")

    lines.append("АКТИВНЫЕ РЕЗЕРВЫ")
    lines.extend(
        "  " + line
        for line in render_table(
            ("Контракт", "Риск, ₽", "ГО, ₽", "Создан", "Возраст", "Состояние"),
            reservations,
        )
    )
    lines.append("")

    lines.append("ПОСЛЕДНИЕ ОТКАЗЫ БИРЖИ")
    lines.extend(
        "  " + line
        for line in render_table(
            ("Время", "Действие", "Контракт", "Причина"),
            rejections,
        )
    )
    lines.append("")
    lines.append(
        "Отказы на допуске (по лимиту риска или правилам профиля) в базе не "
        "сохраняются — их видно в логе робота и в сообщениях."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Консольный отчёт о состоянии журнала сделок (только чтение)."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"путь к базе журнала (по умолчанию {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--rejections",
        type=int,
        default=10,
        help="сколько последних отказов биржи показать (по умолчанию 10)",
    )
    arguments = parser.parse_args(argv)

    try:
        connection = open_readonly(arguments.database)
    except StateUnavailable as error:
        print(str(error), file=sys.stderr)
        return 1

    try:
        now = datetime.now(MSK)
        report = build_report(
            connection,
            read_names(connection),
            load_limits(),
            now,
            arguments.rejections,
        )
    finally:
        connection.close()
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
