"""Предзагрузка расписания исторического прогона и предупреждение о пропусках.

До первого тика расписание каждого инструмента прогона загружается на его
участок диапазона одним проходом. Если сессионная защита внутридневных
сделок включена, а у части дат нет применимого дня, пользователь узнаёт об
этом сразу, а не из отчёта через несколько часов: внутридневные входы в эти
даты будут отклонены с ``session-schedule-unknown``. Прогон продолжается.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.history.preflight import instrument_assignments
from src.logging_setup import get_logger
from src.market_calendar.intervals import exchange_date

log = get_logger(__name__)

# Запас вокруг участка: окно ожидания свечей и сессии выходного дня у границ.
MARGIN_DAYS = 7


@dataclass(frozen=True)
class ScheduleRange:
    instrument_uid: str
    first: date
    last: date
    intraday: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleNotice:
    level: str  # "warning" | "info"
    text: str


def schedule_ranges(selection, instruments, start: datetime, end: datetime, intraday, share, future):
    """Участки диапазона по UID: цепочки фьючерсов — по своим сегментам, прочие — весь прогон."""
    ranges: list[ScheduleRange] = []
    chained: set[str] = set()
    for chain in getattr(selection, "chains", ()) or ():
        for segment in chain:
            instrument = segment.card.instrument
            chained.add(segment.card.uid)
            ranges.append(_range(segment.card.uid, segment.start, segment.end, instrument, intraday, share, future))
    for instrument in instruments:
        uid = getattr(instrument, "uid", None)
        if isinstance(uid, str) and uid and uid not in chained:
            ranges.append(_range(uid, start, end, instrument, intraday, share, future))
    return tuple(ranges)


def _range(uid, start, end, instrument, intraday, share, future) -> ScheduleRange:
    labels = tuple(sorted({
        f"{assignment.id} ({assignment.timeframe})"
        for assignment in instrument_assignments(instrument, share, future)
        if intraday.is_intraday(assignment.timeframe)
    }))
    # Конец диапазона исключён: момент ровно на полуночи МСК в участок не входит.
    last = exchange_date(end - timedelta(microseconds=1)) if end > start else exchange_date(start)
    return ScheduleRange(uid, exchange_date(start), last, labels)


def preload_history_schedule(calendar, ranges, now, intraday) -> ScheduleNotice | None:
    """Загружает расписание участков и возвращает сообщение для консоли и лога."""
    if calendar is None or not ranges:
        return None
    calendar.preload(
        [(item.instrument_uid, item.first - timedelta(days=MARGIN_DAYS), item.last + timedelta(days=MARGIN_DAYS))
         for item in ranges],
        now,
    )
    by_provenance: dict[str, set[date]] = {}
    rules: set[str] = set()
    missing: set[date] = set()
    affected: set[str] = set()
    for item in ranges:
        coverage = calendar.coverage(item.instrument_uid, item.first, item.last)
        rules.update(coverage.rules_versions)
        for value, provenance in coverage.days.items():
            by_provenance.setdefault(provenance, set()).add(value)
        if coverage.missing_dates and item.intraday:
            missing.update(coverage.missing_dates)
            affected.update(item.intraday)
    if intraday.enabled and missing and affected:
        text = (
            f"⚠ Расписание торгов: {len(missing)} {_dates_word(len(missing))} без расписания "
            f"({min(missing)} — {max(missing)}).\n"
            f"  Внутридневные входы в эти даты будут отклонены: {', '.join(sorted(affected))}."
        )
        return ScheduleNotice("warning", text)
    parts = []
    if by_provenance.get("broker"):
        parts.append(f"подтверждено брокером — {len(by_provenance['broker'])} дн.")
    if by_provenance.get("reconstructed"):
        suffix = f" ({', '.join(sorted(rules))})" if rules else ""
        parts.append(f"восстановлено — {len(by_provenance['reconstructed'])} дн.{suffix}")
    if not parts:
        return None
    return ScheduleNotice("info", "Расписание торгов: " + ", ".join(parts))


def report_notice(notice: ScheduleNotice | None) -> None:
    if notice is None:
        return
    print(notice.text)
    if notice.level == "warning":
        log.warning(notice.text)
    else:
        log.info(notice.text)


def _dates_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "дата"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "даты"
    return "дат"
