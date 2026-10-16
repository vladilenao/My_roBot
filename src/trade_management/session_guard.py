"""Сессионная защита внутридневных сделок — чистые решения от расписания и времени.

Блок — слитые соседние окна разрешённых типов сессий одной торговой даты.
Для блока с концом ``E`` рубеж входа ``E − no_entry_before_close_min``,
рубеж закрытия ``E − flatten_before_close_min``. Менеджер сделок только
применяет результат: недопуск входа, отмена ожидающего входа, закрытие или
перенос стопа в безубыток всей сделки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.market_calendar.intervals import exchange_date
from src.trade_management.intraday_settings import SESSION_RAW_TYPES, IntradaySettings
from src.trade_management.models import TradePlan, TradeState
from src.trade_management.profiles.rules import whole_trade_break_even

ALGORITHM = "session.intraday"
ALGORITHM_VERSION = "1"
SESSION_CLOSED = "intraday-session-closed"
CLOSE_NEAR = "intraday-close-near"
SCHEDULE_UNKNOWN = "session-schedule-unknown"
SESSION_END = "intraday-session-end"
CARRY_BREAK_EVEN = "intraday-carry-break-even"
CARRY_STATE_KEY = "intraday_carry_at"
_MERGE_GAP = timedelta(seconds=60)
_KNOWN_SESSION_TYPES = frozenset().union(*SESSION_RAW_TYPES.values())
_LOOKBACK_DAYS = 14


def naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _ceil_minute(moment: datetime) -> datetime:
    floored = moment.replace(second=0, microsecond=0)
    return floored if floored == moment else floored + timedelta(minutes=1)


@dataclass(frozen=True)
class SessionBlock:
    """Непрерывный отрезок разрешённых сессий (naive UTC), конец округлён вверх до минуты."""

    trading_date: date
    start: datetime
    end: datetime
    raw_types: tuple[str, ...]

    def entry_cutoff(self, settings: IntradaySettings) -> datetime:
        return self.end - timedelta(minutes=settings.no_entry_before_close_min)

    def flatten_at(self, settings: IntradaySettings) -> datetime:
        return self.end - timedelta(minutes=settings.flatten_before_close_min)


@dataclass(frozen=True)
class DaySchedule:
    """Блоки торговой даты; ``known`` ложно без применимого типизированного дня."""

    trading_date: date
    known: bool
    blocks: tuple[SessionBlock, ...] = ()
    version: str | None = None


def day_schedule(calendar, trading_date: date, settings: IntradaySettings) -> DaySchedule:
    day = None if calendar is None else calendar.day(trading_date)
    if day is None or not day.is_trading_day:
        return DaySchedule(trading_date, known=False, version=None if day is None else day.version)
    if not any(window.raw_type in _KNOWN_SESSION_TYPES for window in day.windows):
        return DaySchedule(trading_date, known=False, version=day.version)
    allowed = settings.raw_types
    windows = sorted((w for w in day.windows if w.raw_type in allowed), key=lambda w: w.start)
    blocks: list[SessionBlock] = []
    for window in windows:
        end = _ceil_minute(window.end)
        if blocks and window.start - blocks[-1].end <= _MERGE_GAP:
            last = blocks[-1]
            blocks[-1] = SessionBlock(last.trading_date, last.start, max(last.end, end),
                                      last.raw_types + (window.raw_type,))
        else:
            blocks.append(SessionBlock(trading_date, window.start, end, (window.raw_type,)))
    return DaySchedule(trading_date, known=True, blocks=tuple(blocks), version=day.version)


@dataclass(frozen=True)
class EntryDecision:
    """Допуск нового внутридневного входа в момент ``now``; ``code`` None — правило пропускает."""

    now: datetime
    schedule: DaySchedule
    code: str | None
    block: SessionBlock | None = None

    @property
    def allowed(self) -> bool:
        return self.code is None


def entry_decision(calendar, now: datetime, settings: IntradaySettings) -> EntryDecision:
    now = naive_utc(now)
    schedule = day_schedule(calendar, exchange_date(now), settings)
    if not schedule.known:
        return EntryDecision(now, schedule, SCHEDULE_UNKNOWN)
    block = next((b for b in schedule.blocks if b.start <= now < b.end), None)
    if block is None:
        return EntryDecision(now, schedule, SESSION_CLOSED)
    if now >= block.entry_cutoff(settings):
        return EntryDecision(now, schedule, CLOSE_NEAR, block)
    return EntryDecision(now, schedule, None, block)


@dataclass(frozen=True)
class HoldingDecision:
    """Нужно ли сейчас закрыть или перенести внутридневную позицию.

    ``obligated`` ложно внутри безопасного окна блока (от начала до рубежа
    закрытия) и при неизвестном расписании текущей даты. ``last_flatten`` —
    последний наступивший рубеж закрытия: одобрение переноса, данное раньше
    него, уже не действует.
    """

    now: datetime
    schedule: DaySchedule
    obligated: bool
    last_flatten: datetime | None = None
    block: SessionBlock | None = None

    def carry_valid(self, approved_at: datetime | None) -> bool:
        if approved_at is None:
            return False
        return self.last_flatten is None or naive_utc(approved_at) >= self.last_flatten


def holding_decision(calendar, now: datetime, settings: IntradaySettings) -> HoldingDecision:
    now = naive_utc(now)
    today = exchange_date(now)
    schedule = day_schedule(calendar, today, settings)
    if not schedule.known:
        return HoldingDecision(now, schedule, obligated=False)
    for block in schedule.blocks:
        if block.start <= now < block.flatten_at(settings):
            return HoldingDecision(now, schedule, obligated=False, block=block)
    last_flatten, last_block = None, None
    for offset in range(_LOOKBACK_DAYS + 1):
        day = schedule if offset == 0 else day_schedule(calendar, today - timedelta(days=offset), settings)
        passed = [b for b in day.blocks if b.flatten_at(settings) <= now]
        if passed:
            last_block = max(passed, key=lambda b: b.end)
            last_flatten = last_block.flatten_at(settings)
            break
    return HoldingDecision(now, schedule, obligated=True, last_flatten=last_flatten, block=last_block)


@dataclass(frozen=True)
class CarryDecision:
    """Решение на рубеже закрытия: ``keep``, ``move`` (стоп в BE) или ``close``."""

    action: str
    reason: str
    first_fixation: bool
    break_even: Decimal | None = None


def first_fixation(plan: TradePlan, state: TradeState) -> bool:
    """Первая фиксация прибыли: цель tp-1; у ma_cloud без целей — частичный выход в плюс."""
    if "tp-1" in state.completed_target_ids:
        return True
    return (plan.profile.name == "ma_cloud" and state.realized_pnl > 0
            and 0 < state.quantity < state.max_quantity)


def carry_decision(
    plan: TradePlan,
    state: TradeState,
    *,
    close: Decimal | None,
    price_step: Decimal | None,
    step_cost: Decimal | None,
    settings: IntradaySettings,
) -> CarryDecision:
    if not settings.keep_if_tp1_and_breakeven:
        return CarryDecision("close", "carry-disabled", first_fixation=False)
    fixed = first_fixation(plan, state)
    if not fixed:
        return CarryDecision("close", "no-first-fixation", first_fixation=False)
    if (not state.fees_known or plan.cost_snapshot is None or state.average_price is None
            or state.quantity <= 0 or price_step is None or step_cost is None):
        return CarryDecision("close", "costs-unknown", first_fixation=True)
    try:
        break_even = whole_trade_break_even(
            state.average_price, plan.side, price_step, step_cost, state.quantity,
            realized_gross=state.realized_pnl, paid_fees=state.fees,
            expected_exit_cost=plan.cost_snapshot.future_exit * state.quantity)
    except ValueError:
        return CarryDecision("close", "break-even-price-unusable", first_fixation=True)
    direction = Decimal(1) if plan.side == "BUY" else Decimal(-1)
    stop = state.confirmed_stop
    if stop is not None and direction * (stop - break_even) >= 0:
        return CarryDecision("keep", "stop-at-break-even", True, break_even)
    if close is not None and direction * (Decimal(str(close)) - break_even) > 0:
        return CarryDecision("move", "move-stop-to-break-even", True, break_even)
    return CarryDecision("close", "market-beyond-break-even", True, break_even)
