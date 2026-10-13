"""Контракт торговых интервалов брокера: тип → kind и построение окон дня.

Таблица версионируется: изменение классификации меняет
`INTERVAL_KIND_TABLE_VERSION`. Источник — сохранённые сырые ответы
`TradingSchedules` (см. `tests/unit/market_calendar/fixtures`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Literal

IntervalKind = Literal["continuous", "auction", "clearing", "intersession", "unknown"]

INTERVAL_KIND_TABLE: dict[str, IntervalKind] = {
    # непрерывные торги: основная, утренняя, вечерняя, сессии выходного дня, дилерские
    "regular_trading_session": "continuous",
    "regular_trading_session_morning": "continuous",
    "regular_trading_session_main": "continuous",
    "regular_trading_session_evening": "continuous",
    "dealer_regular_trading_session": "continuous",
    "dealer_holiday_trading_session_morning": "continuous",
    "dealer_holiday_trading_session_evening": "continuous",
    "holiday_trading_session_morning": "continuous",
    # аукционы открытия/закрытия и праздничные аукционы
    "opening_auction": "auction",
    "opening_auction_morning": "auction",
    "holiday_opening_auction_morning": "auction",
    "closing_auction": "auction",
    "closing_auction_main": "auction",
    # клиринг
    "clearing": "clearing",
}

# Родовые алиасы, дублирующие более конкретные окна того же диапазона.
GENERIC_INTERVAL_TYPES = frozenset(
    {"regular_trading_session", "dealer_regular_trading_session", "opening_auction", "closing_auction"}
)


def _table_version() -> str:
    payload = "\n".join(f"{name}={kind}" for name, kind in sorted(INTERVAL_KIND_TABLE.items()))
    return sha256(payload.encode("utf-8")).hexdigest()[:12]


INTERVAL_KIND_TABLE_VERSION = _table_version()


@dataclass(frozen=True)
class SessionWindow:
    """Интервал одной торговой даты в UTC-naive с исходным типом ответа."""

    start: datetime
    end: datetime
    kind: IntervalKind
    raw_type: str


def interval_kind(raw_type: object) -> IntervalKind:
    """Классифицирует тип интервала; нераспознанный — ``unknown``."""
    return INTERVAL_KIND_TABLE.get(str(raw_type or ""), "unknown")


def _utc_naive(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def trading_date(day: object) -> date:
    """Торговая дата дня без сдвига из-за часового пояса площадки.

    Брокер отдаёт `TradingDay.date` как 00:00 UTC; берём календарную дату
    напрямую, не нормализуя по локальному поясу.
    """
    value = getattr(day, "date", None)
    if not isinstance(value, datetime):
        raise ValueError("TradingDay.date отсутствует или не datetime")
    return _utc_naive(value).date()


# Режимы торгов проекта (MOEX, FORTS) живут по МСК без перехода на летнее время;
# метка ``TradingDay.date`` (00:00 UTC) — дата по МСК (проба 8.5).
EXCHANGE_UTC_OFFSET = timedelta(hours=3)


def exchange_date(moment: datetime) -> date:
    """Торговая дата момента в поясе площадки, а не по UTC.

    С 21:00 до 24:00 UTC по МСК уже следующие сутки: окно субботней дилерской
    сессии начинается в 23:00 UTC пятницы.
    """
    return (_utc_naive(moment) + EXCHANGE_UTC_OFFSET).date()


def normalize_exchange(value: object) -> str:
    """Код режима: в карточке инструмента строчный, в TradingSchedules — прописной."""
    return str(value or "").strip().lower()


def _from_intervals(day: object) -> list[SessionWindow]:
    windows: list[SessionWindow] = []
    for item in getattr(day, "intervals", ()) or ():
        window = getattr(item, "interval", None)
        start = _utc_naive(getattr(window, "start_ts", None))
        end = _utc_naive(getattr(window, "end_ts", None))
        if start is None or end is None or end <= start:
            continue
        raw_type = str(getattr(item, "type", "") or "")
        windows.append(SessionWindow(start, end, interval_kind(raw_type), raw_type))
    return _dedupe(windows)


def _dedupe(windows: list[SessionWindow]) -> list[SessionWindow]:
    """Схлопывает родовые алиасы, оставляя конкретный тип того же диапазона."""
    best: dict[tuple[datetime, datetime, str], SessionWindow] = {}
    for window in windows:
        key = (window.start, window.end, window.kind)
        current = best.get(key)
        if current is None:
            best[key] = window
        elif current.raw_type in GENERIC_INTERVAL_TYPES and window.raw_type not in GENERIC_INTERVAL_TYPES:
            best[key] = window
    return sorted(best.values(), key=lambda w: (w.start, w.end, w.raw_type))


def _named_field_windows(day: object) -> list[SessionWindow]:
    """Строит окна из именованных полей, когда `intervals` пуст."""
    windows: list[SessionWindow] = []

    def add(start_field: str, end_field: str, kind: IntervalKind, raw_type: str) -> None:
        start = _utc_naive(getattr(day, start_field, None))
        end = _utc_naive(getattr(day, end_field, None))
        if start is not None and end is not None and end > start:
            windows.append(SessionWindow(start, end, kind, raw_type))

    add("premarket_start_time", "premarket_end_time", "continuous", "premarket")
    add("opening_auction_start_time", "opening_auction_end_time", "auction", "opening_auction")
    add("evening_opening_auction_start_time", "evening_start_time", "auction", "evening_opening_auction")
    add("closing_auction_start_time", "closing_auction_end_time", "auction", "closing_auction")
    add("evening_start_time", "evening_end_time", "continuous", "evening")

    main_start = _utc_naive(getattr(day, "start_time", None))
    main_end = _utc_naive(getattr(day, "end_time", None))
    clearing_start = _utc_naive(getattr(day, "clearing_start_time", None))
    clearing_end = _utc_naive(getattr(day, "clearing_end_time", None))
    if main_start is not None and main_end is not None and main_end > main_start:
        segments = [(main_start, main_end)]
        cut_start = max(main_start, clearing_start) if clearing_start is not None else None
        cut_end = min(main_end, clearing_end) if clearing_end is not None else None
        if cut_start is not None and cut_end is not None and cut_end > cut_start:
            remaining: list[tuple[datetime, datetime]] = []
            for seg_start, seg_end in segments:
                if cut_end <= seg_start or cut_start >= seg_end:
                    remaining.append((seg_start, seg_end))
                    continue
                if seg_start < cut_start:
                    remaining.append((seg_start, cut_start))
                if cut_end < seg_end:
                    remaining.append((cut_end, seg_end))
            segments = remaining
            windows.append(SessionWindow(cut_start, cut_end, "clearing", "clearing_start_time"))
        for seg_start, seg_end in segments:
            windows.append(SessionWindow(seg_start, seg_end, "continuous", "start_time"))
    return sorted(windows, key=lambda w: (w.start, w.end, w.raw_type))


def windows_for_day(day: object) -> tuple[SessionWindow, ...]:
    """Окна торговой даты: из `intervals`, иначе из именованных полей."""
    intervals = getattr(day, "intervals", ()) or ()
    if intervals:
        return tuple(_from_intervals(day))
    return tuple(_named_field_windows(day))
