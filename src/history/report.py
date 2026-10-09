"""Отчёт исторического прогона.

Итог по прибыли и убытку берётся из базы журнала прогона (таблица ``account``,
которую ведёт ``ExecutionReducer``), а не пересчитывается заново: так отчёт
гарантированно совпадает с журналом сделок. Пишется и при штатном завершении
прогона, и при аварийной остановке.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.config import DATABASE_FILE
from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)

REPORT_TXT = "report.txt"
REPORT_JSON = "report.json"


def _stamp(moment) -> str:
    if moment is None:
        return ""
    return to_naive(moment).strftime("%Y-%m-%d %H:%M")


def _money(value) -> str:
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):,.2f}".replace(",", " ")


def _plain(value) -> str:
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"


def _span(seconds) -> str:
    """Длительность по-русски, минутной гранулярностью: ``2 ч 11 м``, ``45 м``."""
    total = max(0, int(round(float(seconds or 0) / 60.0)))
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} м"
    if hours:
        return f"{hours} ч"
    return f"{minutes} м"


@dataclass
class RunMetrics:
    """Счётчики прогона: границы, тики, пропуски, разрывы и причина завершения."""

    start: datetime | None = None
    end: datetime | None = None
    ticks: int = 0
    missed_bars: int = 0
    gaps: int = 0
    longest_gap_seconds: float = 0.0
    covered: bool = False
    stop_reason: str = ""
    market_now: datetime | None = None
    crashed: bool = False
    #: Границы реально проверенного горизонта поиска следующего бара — только
    #: когда прогон остановился из-за данных, иначе остаются None.
    horizon_start: datetime | None = None
    horizon_end: datetime | None = None
    horizon_limited: bool = False


@dataclass
class RunResult:
    """Итог прогона: P&L, число сделок и открытые позиции."""

    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")
    equity: Decimal = Decimal("0")
    trades: int = 0
    filled_orders: int = 0
    fills: int = 0
    open_positions: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        data = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in data.items()
        }


def collect_result(storage, short_names: dict[str, str] | None = None) -> RunResult:
    """Читает итог прогона из базы журнала: P&L, сделки, открытые позиции."""
    connection = storage.connection
    account = connection.execute(
        "SELECT balance, equity, realized_pnl, fees, net_realized_pnl FROM account WHERE account_id = 1"
    ).fetchone()
    result = RunResult()
    if account is not None:
        result.balance = Decimal(str(account[0]))
        result.equity = Decimal(str(account[1]))
        result.realized_pnl = Decimal(str(account[2]))
        result.fees = Decimal(str(account[3]))
        result.net_pnl = Decimal(str(account[4]))
    result.trades = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    result.filled_orders = connection.execute(
        "SELECT COUNT(*) FROM orders WHERE status = 'FILLED'"
    ).fetchone()[0]
    result.fills = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    names = short_names if short_names is not None else storage.instrument_names()
    result.open_positions = [
        f"{names.get(row[0]) or 'контракт не указан'} {row[2]}"
        for row in connection.execute(
            "SELECT trades.instrument_id, positions.trade_id, positions.quantity "
            "FROM positions JOIN trades ON trades.trade_id = positions.trade_id "
            "WHERE positions.quantity != 0 ORDER BY trades.instrument_id"
        )
    ]
    return result


def _lines(metrics: RunMetrics, result: RunResult, source: str) -> list[str]:
    coverage = (
        f"диапазон {_stamp(metrics.start)} — {_stamp(metrics.end)} обработан полностью"
        if metrics.covered
        else (
            f"покрыт {_stamp(metrics.start)} — {_stamp(metrics.market_now)}, "
            f"до конца диапазона ({_stamp(metrics.end)}) не дойдено"
        )
    )
    head = [
        "Исторический прогон торгового робота",
        "=" * 38,
        f"Диапазон:        {_stamp(metrics.start)} — {_stamp(metrics.end)}",
        f"Охват диапазона: {coverage}",
    ]
    if metrics.horizon_start is not None and metrics.horizon_end is not None:
        head.append(
            f"Проверенный горизонт: {_stamp(metrics.horizon_start)} — "
            f"{_stamp(metrics.horizon_end)}"
        )
        if metrics.horizon_limited:
            head.append(
                "Горизонт поиска ограничен семью сутками: после него данные "
                "не проверялись и их отсутствие не утверждается."
            )
    return head + [
        f"Обработано тиков: {metrics.ticks}",
        f"Пропущено баров: {metrics.missed_bars}",
        f"Разрывов данных: {metrics.gaps} (самый длинный {_span(metrics.longest_gap_seconds)})",
        f"Завершение:      {metrics.stop_reason or 'не указано'}",
        f"Рыночный момент: {_stamp(metrics.market_now)}",
        "",
        f"Сделок (trades):  {result.trades}",
        f"Исполнено заявок: {result.filled_orders}",
        f"Исполнений:        {result.fills}",
        f"Реализованный P&L: {_money(result.realized_pnl)}",
        f"Комиссии:         {_money(result.fees)}",
        f"Итог (P&L - комиссии): {_money(result.net_pnl)}",
        f"Баланс:           {_money(result.balance)}",
        f"Капитал:          {_money(result.equity)}",
    ] + (
        ["", "Открытые позиции на момент остановки:"]
        + [f"  • {position}" for position in result.open_positions]
        if result.open_positions
        else ["", "Открытых позиций нет."]
    ) + ["", f"База журнала: {source}"]


def write_report(
    state_dir: Path,
    metrics: RunMetrics,
    result: RunResult,
    *,
    source: str = "",
) -> tuple[Path, Path]:
    """Пишет ``report.txt`` и ``report.json`` в каталог состояния прогона."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    txt_path = state_dir / REPORT_TXT
    json_path = state_dir / REPORT_JSON
    origin = source or str(state_dir / DATABASE_FILE)

    payload = {
        "range": {"start": _stamp(metrics.start), "end": _stamp(metrics.end)},
        "covered": metrics.covered,
        "ticks": metrics.ticks,
        "missed_bars": metrics.missed_bars,
        "gaps": metrics.gaps,
        "longest_gap_seconds": round(float(metrics.longest_gap_seconds or 0.0), 3),
        "stop_reason": metrics.stop_reason,
        "market_now": _stamp(metrics.market_now),
        "crashed": metrics.crashed,
        "checked_horizon": (
            {
                "start": _stamp(metrics.horizon_start),
                "end": _stamp(metrics.horizon_end),
                "limited": bool(metrics.horizon_limited),
            }
            if metrics.horizon_start is not None and metrics.horizon_end is not None
            else None
        ),
        "result": result.to_json(),
        "journal": origin,
    }
    txt_path.write_text(
        "\n".join(_lines(metrics, result, origin)) + "\n", encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info("Отчёт прогона записан: %s", txt_path)
    return txt_path, json_path


def completion_line(txt_path: Path) -> str:
    """Единственная строка о завершении прогона, которую видит пользователь."""
    return f"Прогон завершён. Отчёт: {txt_path}"


def crash_line(metrics: RunMetrics) -> str:
    """Строка об аварийной остановке с рыночным моментом остановки."""
    return f"Прогон аварийно остановлен на {_stamp(metrics.market_now)}"
