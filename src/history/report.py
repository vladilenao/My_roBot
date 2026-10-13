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


@dataclass(frozen=True)
class MarketDataSync:
    """Результат проверки импорта My Robot для одного исторического прогона."""

    status: str = "unconfirmed"
    producer_id: str | None = None
    target_change_id: int | None = None
    after_id: int | None = None
    snapshot_generation: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CalendarScheduleMetadata:
    """Использованное историческим прогоном расписание торгов."""

    source: str
    snapshot_generation: int | None = None
    snapshot_pinned: bool = False
    exchanges: tuple[str, ...] = ()
    day_versions: tuple[tuple[str, str, str], ...] = ()
    missing_dates: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "snapshot_generation": self.snapshot_generation,
            "snapshot_pinned": self.snapshot_pinned,
            "exchanges": list(self.exchanges),
            "day_versions": [
                {"exchange": exchange, "date": day, "version": version}
                for exchange, day, version in self.day_versions
            ],
            "missing_dates": list(self.missing_dates),
            "unavailable_reason": self.unavailable_reason,
        }


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
    market_data_sync: MarketDataSync = field(default_factory=MarketDataSync)
    calendar_schedule: CalendarScheduleMetadata | None = None
    rollover: dict | None = None
    boundary_checks: tuple = ()
    last_processed_bar: datetime | None = None
    terminal_gap: bool = False


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
        "Выбор инструментов: " + ("автоматическая цепочка" if metrics.rollover is not None else "фиксированные инструменты"),
    ]
    sync = metrics.market_data_sync
    head.append(
        "Импорт My Robot: "
        + ("подтверждён" if sync.status == "synchronized" else "не подтверждён")
    )
    head.append(
        "Источник/курсор/снимок: "
        f"{sync.producer_id or 'неизвестен'} / "
        f"{sync.after_id if sync.after_id is not None else '?'}"
        f"/{sync.target_change_id if sync.target_change_id is not None else '?'} / "
        f"{sync.snapshot_generation if sync.snapshot_generation is not None else 'нет'}"
    )
    if sync.reason:
        head.append(f"Причина неподтверждения: {sync.reason}")
    schedule = metrics.calendar_schedule
    if schedule is not None:
        snapshot = (
            f"закреплён ({schedule.snapshot_generation})"
            if schedule.snapshot_pinned
            else "не закреплён"
        )
        head.extend([
            "Расписание торгов:",
            f"Источник: {schedule.source}",
            f"Снимок: {snapshot}",
            "Режимы торгов: " + (", ".join(schedule.exchanges) or "нет"),
        ])
        if schedule.day_versions:
            head.append("Версии дней:")
            head.extend(
                f"  {exchange} {day}: {version}"
                for exchange, day, version in schedule.day_versions
            )
        if schedule.missing_dates:
            head.append("Даты без расписания: " + ", ".join(schedule.missing_dates))
        if schedule.unavailable_reason:
            head.append(f"Причина недоступности расписания: {schedule.unavailable_reason}")
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
    if metrics.boundary_checks:
        head.append("Проверенные правые границы:")
        for check in metrics.boundary_checks:
            head.append("  "+check.message())
    if metrics.last_processed_bar is not None:
        head.append(f"Последний обработанный бар: {_stamp(metrics.last_processed_bar)}")
    if metrics.terminal_gap:
        head.append("Конец диапазона внутри подтверждённого перерыва; это не доказательство отсутствия потерь данных.")
    if metrics.rollover is not None:
        plan = metrics.rollover
        head.extend(["", "Фьючерсы: calendar / close-and-wait",
                     f"Семейства: {', '.join(plan['families'])}",
                     f"Каталог: {plan['catalog_digest']}",
                     "Экономические метаданные: статичная карточка источника."])
        for segment in plan["segments"]:
            head.append(f"  {segment['contract']}: {segment['start']} — {segment['end']}; "
                        f"N={segment['lead_days']}; граница={segment['deadline_source']}")
        for event in plan.get("transitions", []):
            status = {"active": "активирован", "draining": "закрытие старого", "waiting_data": "ожидание данных",
                      "skipped": "участок не активирован", "incomplete": "не завершён"}.get(event["status"], event["status"])
            head.append(f"  {event['old_contract']} → {event['new_contract']}: {status}; "
                        f"план={event['cutoff_at']}, закрыт={event['old_flat_at'] or 'не подтверждено'}, "
                        f"активация={event['activation_at'] or 'не состоялась'}; "
                        f"комиссии закрытия={event.get('exit_fees', '0')}; "
                        f"отклонение исполнения, ₽={event.get('exit_slippage_amount') if event.get('exit_slippage_amount') is not None else 'не определено'}")
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
        "market_data_sync": asdict(metrics.market_data_sync),
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
        "rollover": metrics.rollover,
        "selection_mode": "automatic" if metrics.rollover is not None else "fixed",
        "boundary_checks": [item.payload() for item in metrics.boundary_checks],
        "last_processed_bar": _stamp(metrics.last_processed_bar),
        "terminal_gap": metrics.terminal_gap,
    }
    if metrics.calendar_schedule is not None:
        payload["calendar_schedule"] = metrics.calendar_schedule.to_json()
    txt_path.write_text(
        "\n".join(_lines(metrics, result, origin)) + "\n", encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info("Отчёт прогона записан: %s", txt_path)
    return txt_path, json_path


def collect_rollover(storage, selection):
    """Факты и расходы из SQLite, не повторный расчёт PnL координатором."""
    result = selection.payload()
    result.pop("catalog")  # Карточки остаются в contract_chain.json.
    result["transitions"] = []
    for (payload,) in storage.connection.execute("SELECT payload_json FROM rollover_transitions ORDER BY rowid"):
        event = json.loads(payload)
        exit_rows = storage.connection.execute(
            "SELECT f.trade_id,f.quantity,f.price,f.fee,f.executed_at,f.slippage_amount,o.action_type FROM rollover_commands rc "
            "JOIN orders o USING(command_id) JOIN fills f USING(command_id) "
            "WHERE rc.transition_id=? AND (o.action_type IN ('CLOSE','REDUCE','STOP') OR o.action_type LIKE 'TARGET:%') "
            "AND julianday(f.executed_at)>=julianday(?) ORDER BY f.executed_at,f.fill_id",
            (event["transition_id"], event["cutoff_at"])).fetchall()
        event["closed_trades"] = sorted({row[0] for row in exit_rows})
        event["exit_fees"] = str(sum((Decimal(row[3]) for row in exit_rows), Decimal(0)))
        event["exit_slippage_amount"] = (None if any(row[5] is None for row in exit_rows)
            else str(sum((Decimal(row[5]) for row in exit_rows), Decimal(0))))
        event["exits"] = [{"trade_id": row[0], "quantity": row[1], "price": row[2], "fee": row[3], "executed_at": row[4], "action_type": row[6]} for row in exit_rows]
        cutoff = to_naive(event["cutoff_at"])
        for key in ("draining_at", "old_flat_at", "activation_at"):
            event[f"{key}_delay_seconds"] = None if not event[key] else max(0, (to_naive(event[key])-cutoff).total_seconds())
        result["transitions"].append(event)
    return result


def completion_line(txt_path: Path) -> str:
    """Единственная строка о завершении прогона, которую видит пользователь."""
    return f"Прогон завершён. Отчёт: {txt_path}"


def crash_line(metrics: RunMetrics) -> str:
    """Строка об аварийной остановке с рыночным моментом остановки."""
    return f"Прогон аварийно остановлен на {_stamp(metrics.market_now)}"
