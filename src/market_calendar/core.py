"""Чистая модель расписания торгов и расчёт свечных ожиданий.

Модуль не читает часы, сеть или SQLite. Все входные моменты нормализуются в
UTC-naive, поэтому сравнения не зависят от часового пояса источника.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from typing import Literal

from src.scheduler.timing import CandleScheduler

from .intervals import SessionWindow, exchange_date
from .rules import CandleRule

ScheduleSource = Literal["tbank", "emulator"]
NextEventKind = Literal[
    "bar_close",
    "session_open",
    "session_close",
    "break_start",
    "break_end",
    "publication_deadline",
    "schedule_refresh",
]


class MarketPhase(str, Enum):
    TRADING = "trading"
    BREAK = "break"
    CLOSED = "closed"
    UNKNOWN = "unknown"


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class CalendarDay:
    """Одна версия расписания торговой даты, уже разобранная в окна."""

    trading_date: date
    is_trading_day: bool
    windows: tuple[SessionWindow, ...]
    version: str
    observed_at: datetime
    provenance: str = "broker"
    rules_version: str | None = None

    def __post_init__(self) -> None:
        windows = tuple(
            SessionWindow(_utc_naive(window.start), _utc_naive(window.end), window.kind, window.raw_type)
            for window in self.windows
        )
        if any(window.end <= window.start for window in windows):
            raise ValueError("Окно расписания должно иметь положительную длительность")
        object.__setattr__(self, "windows", tuple(sorted(windows, key=lambda w: (w.start, w.end, w.raw_type))))
        object.__setattr__(self, "observed_at", _utc_naive(self.observed_at))


@dataclass(frozen=True)
class ScheduleRef:
    source: ScheduleSource
    exchange: str
    days: tuple[tuple[date, str], ...]
    digest: str
    observed_at: datetime
    snapshot_generation: int | None
    stale: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc_naive(self.observed_at))


@dataclass(frozen=True)
class DueBar:
    open: datetime
    close: datetime
    publication_deadline: datetime
    rule_confirmed: bool

    def __post_init__(self) -> None:
        opened = _utc_naive(self.open)
        closed = _utc_naive(self.close)
        deadline = _utc_naive(self.publication_deadline)
        if closed <= opened:
            raise ValueError("Закрытие свечи должно быть позже открытия")
        if deadline < closed:
            raise ValueError("Срок публикации не может быть раньше закрытия свечи")
        object.__setattr__(self, "open", opened)
        object.__setattr__(self, "close", closed)
        object.__setattr__(self, "publication_deadline", deadline)


@dataclass(frozen=True)
class NextEvent:
    kind: NextEventKind
    at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _utc_naive(self.at))


@dataclass(frozen=True)
class CandleExpectation:
    instrument_uid: str
    timeframe: str
    now: datetime
    phase: MarketPhase
    break_kind: str | None
    last_due: DueBar | None
    next_event: NextEvent | None
    schedule: ScheduleRef | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _utc_naive(self.now))


@dataclass(frozen=True)
class TradingCalendar:
    """Применимые дни одного режима торгов без источника хранения."""

    source: ScheduleSource
    exchange: str
    days: tuple[CalendarDay, ...]
    stale: bool = False
    snapshot_generation: int | None = None

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.days, key=lambda day: day.trading_date))
        if len({day.trading_date for day in ordered}) != len(ordered):
            raise ValueError("В календаре должна быть одна применимая версия на торговую дату")
        object.__setattr__(self, "days", ordered)

    def day(self, value: date) -> CalendarDay | None:
        return next((day for day in self.days if day.trading_date == value), None)

    def schedule_ref(self, used_days: set[date]) -> ScheduleRef | None:
        days = tuple((day.trading_date, day.version) for day in self.days if day.trading_date in used_days)
        if not days:
            return None
        payload = "\n".join(f"{day.isoformat()}:{version}" for day, version in days)
        observed_at = min(day.observed_at for day in self.days if day.trading_date in used_days)
        return ScheduleRef(
            source=self.source,
            exchange=self.exchange,
            days=days,
            digest=sha256(payload.encode("utf-8")).hexdigest()[:12],
            observed_at=observed_at,
            snapshot_generation=self.snapshot_generation,
            stale=self.stale,
        )


@dataclass(frozen=True)
class _PhaseState:
    phase: MarketPhase
    break_kind: str | None
    reason: str
    phase_event: NextEvent | None
    used_days: set[date]


def _window_is_trading(window: SessionWindow) -> bool:
    return window.kind in ("continuous", "unknown")


def _windows(calendar: TradingCalendar) -> tuple[tuple[date, SessionWindow], ...]:
    return tuple((day.trading_date, window) for day in calendar.days for window in day.windows)


def _phase_at(calendar: TradingCalendar, now: datetime) -> _PhaseState:
    current = calendar.day(exchange_date(now))
    all_windows = _windows(calendar)
    active = [(day, window) for day, window in all_windows if window.start <= now < window.end]
    if active:
        trading = next(((day, window) for day, window in active if _window_is_trading(window)), None)
        if trading is not None:
            day, window = trading
            later = any(other_day == day and other.start >= window.end for other_day, other in all_windows)
            event_kind: NextEventKind = "break_start" if later else "session_close"
            reason = "unknown_interval_type" if window.kind == "unknown" else ""
            return _PhaseState(
                MarketPhase.TRADING,
                None,
                reason,
                NextEvent(event_kind, window.end),
                {day},
            )
        day, window = min(active, key=lambda item: item[1].end)
        return _PhaseState(
            MarketPhase.BREAK,
            window.kind,
            "",
            NextEvent("break_end", window.end),
            {day},
        )

    if current is None:
        return _PhaseState(MarketPhase.UNKNOWN, None, "schedule_missing_date", None, set())
    if not current.is_trading_day:
        future = _next_window(all_windows, now)
        event = NextEvent("session_open", future[1].start) if future is not None else None
        used = {current.trading_date} | ({future[0]} if future is not None else set())
        reason = "" if event is not None else "next_session_unknown"
        return _PhaseState(MarketPhase.CLOSED, None, reason, event, used)
    if not current.windows:
        return _PhaseState(MarketPhase.UNKNOWN, None, "trading_day_without_windows", None, {current.trading_date})

    earlier = [window for window in current.windows if window.end <= now]
    later = [window for window in current.windows if window.start > now]
    if earlier and later:
        next_window = min(later, key=lambda window: window.start)
        return _PhaseState(
            MarketPhase.BREAK,
            "intersession",
            "",
            NextEvent("break_end", next_window.start),
            {current.trading_date},
        )

    future = _next_window(all_windows, now)
    event = NextEvent("session_open", future[1].start) if future is not None else None
    used = {current.trading_date} | ({future[0]} if future is not None else set())
    reason = "" if event is not None else "next_session_unknown"
    return _PhaseState(MarketPhase.CLOSED, None, reason, event, used)


def _next_window(
    windows: tuple[tuple[date, SessionWindow], ...], now: datetime
) -> tuple[date, SessionWindow] | None:
    future = ((day, window) for day, window in windows if window.start > now)
    return min(future, key=lambda item: item[1].start, default=None)


def _intersects_trading(calendar: TradingCalendar, opened: datetime, closed: datetime) -> tuple[bool, set[date]]:
    used_days: set[date] = set()
    for day, window in _windows(calendar):
        if _window_is_trading(window) and opened < window.end and window.start < closed:
            used_days.add(day)
    return bool(used_days), used_days


def _due_bar(rule: CandleRule, grid: CandleScheduler, opened: datetime, timeout: timedelta) -> DueBar:
    closed = grid.bar_close(opened)
    return DueBar(opened, closed, closed + timeout, rule.rule_confirmed)


def _trading_windows(calendar: TradingCalendar) -> tuple[tuple[date, SessionWindow], ...]:
    return tuple((day, window) for day, window in _windows(calendar) if _window_is_trading(window))


def _last_due(calendar: TradingCalendar, rule: CandleRule, now: datetime, timeout: timedelta) -> tuple[DueBar | None, set[date]]:
    """Самый поздний закрытый к ``now`` бар, пересекающий торговое окно.

    Перебираются окна, а не бары: в каждом окне кандидат — бар, содержащий
    последний момент окна до ``now``; закрытие — по сетке ТФ.
    """
    grid = CandleScheduler(rule.timeframe)
    best: datetime | None = None
    for _, window in _trading_windows(calendar):
        if window.start >= now:
            continue
        point = min(window.end, now) - timedelta(microseconds=1)
        opened = grid.current_candle_start(point)
        if grid.bar_close(opened) > now:
            opened = grid.previous_candle_start(opened)
        if grid.bar_close(opened) <= window.start:
            continue
        if best is None or opened > best:
            best = opened
    if best is None:
        return None, set()
    due = _due_bar(rule, grid, best, timeout)
    _, used = _intersects_trading(calendar, due.open, due.close)
    return due, used


def _next_due(calendar: TradingCalendar, rule: CandleRule, now: datetime, timeout: timedelta) -> tuple[DueBar | None, set[date]]:
    """Ближайший ещё не закрытый бар, пересекающий торговое окно."""
    grid = CandleScheduler(rule.timeframe)
    best: datetime | None = None
    for _, window in _trading_windows(calendar):
        if window.end <= now:
            continue
        opened = grid.current_candle_start(max(window.start, now))
        if best is None or opened < best:
            best = opened
    if best is None:
        return None, set()
    due = _due_bar(rule, grid, best, timeout)
    _, used = _intersects_trading(calendar, due.open, due.close)
    return due, used


def expected_bars(
    calendar: TradingCalendar,
    rule: CandleRule,
    start: datetime,
    end: datetime,
    timeout: timedelta,
) -> tuple[DueBar, ...]:
    """Ожидаемые бары с открытием в полуинтервале ``[start, end)``.

    Функция ничего не знает о полученных свечах. Неподтверждённое правило всё
    равно перечисляет бары для диагностики, но помечает их ``rule_confirmed``.
    Бары перечисляются только внутри торговых окон: закрытые выходные не
    обходятся поминутно.
    """
    start = _utc_naive(start)
    end = _utc_naive(end)
    if end <= start:
        return ()
    grid = CandleScheduler(rule.timeframe)
    opens: set[datetime] = set()
    for _, window in _trading_windows(calendar):
        if window.end <= start or window.start >= end:
            continue
        opened = grid.current_candle_start(max(window.start, start))
        if opened < start:
            opened = grid.bar_close(opened)
        while opened < end and opened < window.end:
            opens.add(opened)
            opened = grid.bar_close(opened)
    return tuple(_due_bar(rule, grid, opened, timeout) for opened in sorted(opens))


def scheduled_closed_eligible(expectation: CandleExpectation, calendar_evidence: bool) -> bool:
    """Разрешает календарное ``scheduled_closed`` только при полном evidence."""
    return (
        calendar_evidence
        and expectation.phase in (MarketPhase.BREAK, MarketPhase.CLOSED)
        and expectation.schedule is not None
        and expectation.last_due is not None
        and expectation.last_due.rule_confirmed
    )


def _previous_session_uncovered(calendar: TradingCalendar, now: datetime) -> bool:
    """Нет ни одной покрытой даты до текущей торговой даты."""
    today = exchange_date(now)
    return not any(day.trading_date < today for day in calendar.days)


def expect(
    calendar: TradingCalendar | None,
    rule: CandleRule,
    now: datetime,
    timeout: timedelta,
    *,
    instrument_uid: str = "",
) -> CandleExpectation:
    """Вычисляет чистое календарное ожидание пары в момент ``now``."""
    now = _utc_naive(now)
    if calendar is None:
        return CandleExpectation(
            instrument_uid,
            rule.timeframe,
            now,
            MarketPhase.UNKNOWN,
            None,
            None,
            None,
            None,
            "schedule_unavailable",
        )

    state = _phase_at(calendar, now)
    if state.phase is MarketPhase.UNKNOWN:
        reasons = [state.reason]
        if not rule.rule_confirmed:
            reasons.append("timeframe_rule_unconfirmed")
        return CandleExpectation(
            instrument_uid,
            rule.timeframe,
            now,
            state.phase,
            None,
            None,
            None,
            calendar.schedule_ref(state.used_days),
            "; ".join(reasons),
        )

    last_due, last_days = _last_due(calendar, rule, now, timeout)
    next_due, next_days = _next_due(calendar, rule, now, timeout)
    used_days = state.used_days | last_days | next_days
    schedule = calendar.schedule_ref(used_days)

    candidates: list[tuple[int, NextEvent]] = []
    if state.phase_event is not None:
        candidates.append((0, state.phase_event))
    if next_due is not None:
        candidates.append((1, NextEvent("bar_close", next_due.close)))
    if last_due is not None and last_due.publication_deadline > now:
        candidates.append((2, NextEvent("publication_deadline", last_due.publication_deadline)))
    next_event = min(candidates, key=lambda item: (item[1].at, item[0]))[1] if candidates else None

    reasons = [state.reason] if state.reason else []
    if last_due is None and _previous_session_uncovered(calendar, now):
        # Прошедшие даты брокер не отдаёт: при холодном старте последний бар
        # прошлой сессии неизвестен и не даёт ни доказательства, ни требования.
        reasons.append("previous_session_unknown")
    if not rule.rule_confirmed:
        reasons.append("timeframe_rule_unconfirmed")
    return CandleExpectation(
        instrument_uid,
        rule.timeframe,
        now,
        state.phase,
        state.break_kind,
        last_due,
        next_event,
        schedule,
        "; ".join(reasons),
    )
