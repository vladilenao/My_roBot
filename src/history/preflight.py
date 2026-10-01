"""Предварительная проверка исторического прогона.

До входа в цикл проверяет, что источник данных отвечает, инструменты
разрешаются, а свечи покрывают диапазон вместе с прогревом. Проблемы
перечисляются пользователю целиком: прогон не начинается, пока есть хотя бы
одна непрочитанная пара «инструмент, таймфрейм».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)


def _period(timeframe: str) -> timedelta:
    seconds = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
    }.get(timeframe)
    if seconds is None:
        raise ValueError(f"Неизвестный таймфрейм для прогрева: {timeframe!r}")
    return timedelta(seconds=seconds)


def smallest_period(timeframes) -> timedelta:
    """Период наименьшего активного таймфрейма — шаг рыночного времени прогона."""
    return min((_period(tf) for tf in timeframes), default=timedelta(minutes=1))


@dataclass(frozen=True)
class Pair:
    """Активная пара прогона: инструмент на таймфрейме."""

    ticker: str
    instrument_type: str
    timeframe: str

    def __str__(self) -> str:
        return f"{self.ticker} {self.timeframe}"


def active_pairs(
    instruments,
    share_strategies: dict,
    future_strategies: dict,
    protection_timeframe: str = "1m",
) -> tuple[Pair, ...]:
    """Пары, которые реально будут грузиться: таймфреймы привязок плюс защита."""
    pairs: list[Pair] = []
    for instrument in instruments:
        if instrument.instrument_type == "future":
            assignments = future_strategies.get(instrument.ticker[:2].upper(), [])
        else:
            assignments = share_strategies.get(instrument.ticker, [])
        timeframes = {assignment.timeframe for assignment in assignments}
        timeframes.add(protection_timeframe)
        for timeframe in sorted(timeframes, key=lambda tf: _period(tf)):
            pairs.append(Pair(instrument.ticker, instrument.instrument_type, timeframe))
    return tuple(pairs)


@dataclass
class PreflightReport:
    """Итог проверки: список проблем и понятное пользователю сообщение."""

    problems: tuple[str, ...] = ()
    checked_pairs: int = 0
    detail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def message(self) -> str:
        if self.ok:
            return f"Проверка пройдена: пар {self.checked_pairs}."
        lines = [
            "Прогон невозможен — данные не прошли предварительную проверку:",
            *(f"  • {problem}" for problem in self.problems),
        ]
        if self.detail:
            lines.append("")
            lines.append("Проверенные пары:")
            lines.extend(f"  - {line}" for line in self.detail)
        return "\n".join(lines)


def _frame_bounds(frame: pd.DataFrame) -> tuple[datetime, datetime]:
    if frame is None or frame.empty:
        raise LookupError("свечи не найдены")
    stamps = pd.to_datetime(frame["datetime"])
    return to_naive(stamps.min()).to_pydatetime(), to_naive(stamps.max()).to_pydatetime()


def run_preflight(
    pairs,
    *,
    start,
    end,
    warmup_bars: int,
    load_candles,
    client_provider=None,
    clock=None,
    ping=None,
) -> PreflightReport:
    """Проверяет источник, разрешение инструментов и покрытие диапазона.

    ``load_candles`` — загрузчик свечей прогона: подставляется тот же, что
    использует кэш, поэтому проверка говорит ровно о тех данных, которые увидит
    прогон. ``client_provider`` и ``clock`` уходят в загрузчик так же, как в
    кэше, иначе проверка считала бы границы от «сейчас». ``ping`` —
    необязательная проверка живости источника.
    """
    start_dt = to_naive(start).to_pydatetime()
    end_dt = to_naive(end).to_pydatetime()
    if end_dt <= start_dt:
        return PreflightReport(problems=("конец диапазона должен быть позже начала",))
    if not pairs:
        return PreflightReport(problems=("не выбрано ни одной активной пары инструментов",))

    problems: list[str] = []
    detail: list[str] = []
    if ping is not None:
        try:
            ping()
        except Exception as exc:  # noqa: BLE001 — причина уходит в сообщение пользователю
            problems.append(f"источник данных недоступен: {exc}")

    step = smallest_period(pair.timeframe for pair in pairs)
    last_bar = end_dt - step
    for pair in pairs:
        line, pair_problems = _check_pair(
            pair,
            start_dt=start_dt,
            end_dt=end_dt,
            last_bar=last_bar,
            step=step,
            warmup_bars=warmup_bars,
            load_candles=load_candles,
            client_provider=client_provider,
            clock=clock,
        )
        detail.append(line)
        problems.extend(pair_problems)

    return PreflightReport(problems=tuple(problems), checked_pairs=len(pairs), detail=detail)


def _check_pair(
    pair: Pair,
    *,
    start_dt: datetime,
    end_dt: datetime,
    last_bar: datetime,
    step: timedelta,
    warmup_bars: int,
    load_candles,
    client_provider,
    clock,
) -> tuple[str, list[str]]:
    period = _period(pair.timeframe)
    warmup_span = max(1, warmup_bars) * period
    head_from = start_dt - warmup_span
    try:
        head, _ = load_candles(
            ticker=pair.ticker,
            instrument_type=pair.instrument_type,
            timeframe=pair.timeframe,
            start_date=head_from,
            end_date=start_dt + period,
            instrument_id=None,
            client_provider=client_provider,
            clock=clock,
        )
    except Exception as exc:
        return f"{pair}: загрузка не удалась ({exc})", [
            f"{pair}: инструмент не разрешается или данные недоступны ({exc})"
        ]

    problems: list[str] = []
    try:
        first_seen, head_last = _frame_bounds(head)
    except LookupError:
        return f"{pair}: нет свечей в начале диапазона", [
            f"{pair}: свечи не покрывают начало диапазона {start_dt:%Y-%m-%d %H:%M}"
        ]

    if head_last < start_dt:
        problems.append(
            f"{pair}: нет бара, открывшегося в {start_dt:%Y-%m-%d %H:%M} — начало диапазона не покрыто"
        )
    if first_seen > head_from + period:
        problems.append(
            f"{pair}: прогрев не покрыт — данные начинаются с {first_seen:%Y-%m-%d %H:%M}, "
            f"нужно с {head_from:%Y-%m-%d %H:%M} ({warmup_bars} бар(ов) {pair.timeframe})"
        )

    try:
        _, tail_last = _frame_bounds(
            load_candles(
                ticker=pair.ticker,
                instrument_type=pair.instrument_type,
                timeframe=pair.timeframe,
                start_date=max(last_bar - period, start_dt),
                end_date=end_dt,
                instrument_id=None,
                client_provider=client_provider,
                clock=clock,
            )[0]
        )
    except LookupError:
        return f"{pair}: нет свечей в конце диапазона", [
            f"{pair}: свечи не покрывают конец диапазона {end_dt:%Y-%m-%d %H:%M} "
            f"(ожидался бар {last_bar:%Y-%m-%d %H:%M})"
        ]

    if tail_last < last_bar:
        problems.append(
            f"{pair}: последний бар {tail_last:%Y-%m-%d %H:%M} раньше ожидаемого "
            f"{last_bar:%Y-%m-%d %H:%M} — конец диапазона не покрыт"
        )
    return f"{pair}: {first_seen:%Y-%m-%d %H:%M} … {tail_last:%Y-%m-%d %H:%M}", problems
