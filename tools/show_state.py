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
    portfolio_pct: Decimal
    commission: Decimal = Decimal("1.5")
    slippage: Decimal = Decimal("1")


def load_limits() -> Limits:
    """Прочитать лимиты из конфигурации робота, а не из констант отчёта."""
    from src.config import RISK_LIMITS

    return Limits(
        portfolio_pct=Decimal(str(RISK_LIMITS["portfolio_pct"])),
        commission=Decimal(str(RISK_LIMITS["commission"])),
        slippage=Decimal(str(RISK_LIMITS["slippage"])),
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
    if version < 9 or version > 10:
        connection.close()
        raise StateUnavailable(
            f"схема базы версии {version}, отчёт поддерживает версии 9–10.\n"
            "Запустите робот один раз на обновлённом коде — он обновит базу и запишет имена контрактов."
        )
    return connection


def read_names(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT ticker, short_name FROM instrument_names").fetchall()
    return {ticker: short_name for ticker, short_name in rows}


def contract_label(ticker: str, names: dict[str, str]) -> str:
    """Показать короткое имя; сырой тикер пользователю не выводится."""
    return names.get(ticker, "контракт не указан")


def _decimal(value, *, positive=False, signed=False) -> Decimal | None:
    """Unknown/invalid amounts stay unknown; zero is a distinct valid fact."""
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0) or (not signed and result < 0):
            return None
        return result
    except (ArithmeticError, ValueError, TypeError):
        return None


def collect_account(connection: sqlite3.Connection) -> tuple[Decimal | None, Decimal | None]:
    row = connection.execute("SELECT balance, equity FROM account WHERE account_id = 1").fetchone()
    if row is None:
        return None, None
    return _decimal(row[0], signed=True), _decimal(row[1], signed=True)


def collect_reservation_totals(connection: sqlite3.Connection):
    rows = connection.execute(
        "SELECT risk_amount, margin_amount "
        "FROM reservations WHERE status = 'ACTIVE'"
    ).fetchall()
    totals, known = [Decimal(0), Decimal(0)], [True, True]
    for row in rows:
        for index, value in enumerate(row):
            amount = _decimal(value)
            if amount is None:
                known[index] = False
            else:
                totals[index] += amount
    return (*totals, *known)


@dataclass(frozen=True)
class Exposure:
    rows: tuple[tuple[str, ...], ...]
    known_risk: Decimal
    known_margin: Decimal
    unknown_risk: tuple[str, ...]
    unknown_margin: tuple[str, ...]


def collect_open_trades(
    connection: sqlite3.Connection, names: dict[str, str], limits: Limits
) -> Exposure:
    from src.portfolio.risk import PortfolioRiskManager, RiskTrade

    placeholders = ", ".join("?" * len(TERMINAL_PHASES))
    rows = connection.execute(
        f"""
        SELECT t.trade_id, t.instrument_id, t.side, t.phase, p.quantity, p.average_price,
               pr.confirmed_stop, pr.pending_stop, t.price_step, t.step_cost, t.plan_json
        FROM trades t
        LEFT JOIN positions p ON p.trade_id = t.trade_id
        LEFT JOIN protection pr ON pr.trade_id = t.trade_id
        WHERE t.phase NOT IN ({placeholders}) OR p.quantity > 0
        ORDER BY t.created_at
        """,
        TERMINAL_PHASES,
    ).fetchall()
    result, unknown_risk, unknown_margin = [], [], []
    open_risk, open_margin = Decimal(0), Decimal(0)
    for trade_id, instrument, side, phase, quantity, average, confirmed, pending, step, cost, payload in rows:
        label = contract_label(instrument, names)
        price_risk, future, risk, source = None, None, None, "—"
        try:
            plan = json.loads(payload)
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
        except (ValueError, TypeError):
            plan = None
        if quantity is None and phase not in {"PLANNED", "ENTRY_PENDING"}:
            unknown_risk.append(f"{label}: неизвестен объём позиции")
            unknown_margin.append(f"{label}: неизвестен объём позиции")
        elif quantity and quantity > 0:
            average_value, stop_value = _decimal(average, positive=True), _decimal(confirmed, positive=True)
            step_value, cost_value = _decimal(step, positive=True), _decimal(cost, positive=True)
            if None not in (average_value, stop_value, step_value, cost_value) and side in {"BUY", "SELL"}:
                direction = Decimal(1) if side == "BUY" else Decimal(-1)
                price_risk = max(Decimal(0), direction*(average_value-stop_value)/step_value*cost_value*quantity)
            snapshot = plan.get("cost_snapshot") if plan is not None else None
            if snapshot is None and plan is not None and plan.get("algorithm_version", "legacy-v1") == "legacy-v1":
                rate, allowance = limits.commission, limits.slippage
                source = "оценка будущих legacy-затрат"
            elif isinstance(snapshot, dict):
                rate, allowance = _decimal(snapshot.get("commission")), _decimal(snapshot.get("slippage"))
                source = "сохранённый снимок расходов"
            else:
                rate, allowance = None, None
            if rate is not None and allowance is not None:
                future = (rate+allowance/2)*quantity
            if price_risk is not None and future is not None:
                factual = RiskTrade(trade_id, instrument, frozenset(), side, quantity,
                    average_value, stop_value, step_value, cost_value,
                    expected_exit_cost=rate*quantity, slippage_allowance=allowance/2*quantity)
                risk = PortfolioRiskManager.trade_risk(factual).remaining_loss
                open_risk += risk
            else:
                open_risk += (price_risk if price_risk is not None else Decimal(0)) + (future if future is not None else Decimal(0))
                unknown_risk.append(f"{label}: неизвестны подтверждённый стоп, денежные факторы или будущие расходы")
            admission = plan.get("admission_snapshot") if plan is not None else None
            go = _decimal(admission.get("go_per_contract")) if isinstance(admission, dict) else None
            if go is None:
                unknown_margin.append(f"{label}: неизвестно ГО открытого остатка")
            else:
                open_margin += go*quantity
        result.append(
            (
                label,
                "ПОКУПКА" if side == "BUY" else "ПРОДАЖА",
                phase,
                str(quantity or 0),
                format_price(average),
                format_price(confirmed),
                format_price(pending) + (" (ожидается)" if pending is not None else ""),
                format_money(price_risk),
                format_money(future),
                format_money(risk),
                source,
            )
        )
    return Exposure(tuple(result), open_risk, open_margin, tuple(unknown_risk), tuple(unknown_margin))


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
                format_money(_decimal(risk)),
                format_money(_decimal(margin)),
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
    # SAVEPOINT also works inside a caller-owned read transaction. All actual
    # report inputs, including names, are read after this snapshot starts.
    connection.execute("SAVEPOINT state_report")
    try:
        return _build_report(connection, read_names(connection), limits, now, rejection_limit)
    finally:
        connection.execute("RELEASE state_report")


def _build_report(connection, names, limits, now, rejection_limit):
    balance, equity = collect_account(connection)
    budget_base = max(Decimal(0), min(balance, equity)) if balance is not None and equity is not None else None
    pending_risk, pending_margin, pending_risk_known, pending_margin_known = collect_reservation_totals(connection)
    risk_budget = budget_base*limits.portfolio_pct/100 if budget_base is not None else None
    exposure = collect_open_trades(connection, names, limits)
    used_risk = exposure.known_risk+pending_risk if not exposure.unknown_risk and pending_risk_known else None
    used_margin = exposure.known_margin+pending_margin if not exposure.unknown_margin and pending_margin_known else None
    free = max(Decimal(0), risk_budget-used_risk) if risk_budget is not None and used_risk is not None else None
    excess = max(Decimal(0), used_risk-risk_budget) if risk_budget is not None and used_risk is not None else None
    reservations = collect_reservations(connection, names, now)
    rejections = collect_broker_rejections(connection, names, rejection_limit)

    lines: list[str] = []
    lines.append(f"Состояние журнала сделок · снимок {format_moment(now)} МСК")
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
        f"  Общий риск портфеля: {format_money(used_risk)} / {format_money(risk_budget)} ₽ "
        f"— общий лимит {format_percent_value(limits.portfolio_pct)}"
    )
    lines.append(f"  Открытый риск с будущими расходами: {format_money(exposure.known_risk) if not exposure.unknown_risk else 'неизвестен'} ₽")
    lines.append(f"  Незаполненные резервы риска: {format_money(pending_risk) if pending_risk_known else 'неизвестны'} ₽")
    lines.append(f"  Свободный риск: {format_money(free)} ₽ · Превышение: {format_money(excess)} ₽")
    if used_risk is None or budget_base is None:
        lines.append(f"  risk-state-unknown: известная часть занятого риска {format_money(exposure.known_risk+pending_risk)} ₽; достоверный свободный бюджет неизвестен")
        lines.extend("  " + reason for reason in exposure.unknown_risk)
        if not pending_risk_known:
            lines.append("  Неизвестны суммы ACTIVE-резервов риска")
        if budget_base is None:
            lines.append("  Неизвестны баланс или эквити счёта")
    if free is None or excess or limits.portfolio_pct == 0:
        lines.append("  Новые входы/доборы запрещены; бюджетное закрытие позиций не выполняется")
    lines.append(
        f"  Гарантийное обеспечение: {format_money(used_margin)} / {format_money(budget_base)} ₽ "
        "— фактические открытые остатки и незаполненные резервы"
    )
    lines.append(
        f"  Известное ГО открытых остатков: {format_money(exposure.known_margin)} ₽ · "
        f"ГО незаполненных резервов: {format_money(pending_margin) if pending_margin_known else 'неизвестно'} ₽"
    )
    lines.extend("  " + reason for reason in exposure.unknown_margin)
    if not pending_margin_known:
        lines.append("  Неизвестны суммы ГО ACTIVE-резервов")
    if used_margin is None:
        lines.append("  Полное обеспечение неизвестно; достоверный допуск увеличений по ГО не подтверждён")
    lines.append("")

    lines.append("СДЕЛКИ В РАБОТЕ")
    lines.extend(
        "  " + line
        for line in render_table(
            ("Контракт", "Сторона", "Фаза", "Объём", "Средняя", "Стоп", "Pending-стоп", "До стопа, ₽", "Выход, ₽", "Риск, ₽", "Расходы"),
            exposure.rows,
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
        "Расчёты допуска и причины неизвестности доступны в аудите; "
        "исторические комиссии в отчёте не переоцениваются."
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
