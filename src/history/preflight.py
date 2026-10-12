"""Предварительная проверка исторического прогона.

До входа в цикл проверяет, что источник данных отвечает, инструменты
разрешаются, а свечи покрывают диапазон вместе с прогревом. Проблемы
перечисляются пользователю целиком: прогон не начинается, пока есть хотя бы
одна непрочитанная пара «инструмент, таймфрейм».

Правая граница проверяется по сетке пары или ограниченному перерыву,
подтверждённому соседними барами той же пары и снимка. Начало в нерабочей точке календаря
(праздник, выходной) не отвергает прогон: начало задаёт драйверный (наименьший
активный) таймфрейм, и если его ближайшая свеча нашлась в ``LOOKAHEAD``, начало
переносится ровно на неё, а прогрев проверяется отдельно. Старшие таймфреймы
берут контекст из прогрева и старт не двигают. Данных, начинающихся заметно
позже, это не касается — такое расхождение остаётся ошибкой, чтобы опечатка в
диапазоне не прошла молча.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

import pandas as pd

from src.data.timeutil import to_naive
from src.logging_setup import get_logger
from src.scheduler.timing import CandleScheduler
from src.history.source import source_identity

log = get_logger(__name__)

LOOKAHEAD = timedelta(days=7)
"""Насколько далеко в обе стороны проверка ищет ближайшую свечу."""

_EPOCH = datetime(1970, 1, 1)


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


def _span(span: timedelta) -> str:
    """Длительность по-русски, минутной гранулярностью: ``2 ч 11 м``, ``45 м``."""
    minutes = max(0, int(round(span.total_seconds() / 60.0)))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} м"
    if hours:
        return f"{hours} ч"
    return f"{rest} м"


@dataclass(frozen=True)
class Pair:
    """Активная пара прогона: инструмент на таймфрейме."""

    ticker: str
    instrument_type: str
    timeframe: str
    instrument_id: str | None = field(default=None, compare=False)
    short_name: str | None = field(default=None, compare=False)

    def __str__(self) -> str:
        name = self.short_name or (self.ticker if self.instrument_type == "share" else "контракт не указан")
        return f"{name} {self.timeframe}"


@dataclass(frozen=True)
class BoundaryCheck:
    """Доказательство границы содержит только метки и идентичности, без OHLCV."""

    pair: Pair
    source_id: str
    snapshot_generation: int | None
    role: str
    boundary: datetime
    expected: datetime
    left_window: tuple[datetime, datetime]
    status: str = "failed"
    last_before: datetime | None = None
    first_after: datetime | None = None
    right_window: tuple[datetime, datetime] | None = None
    gap_seconds: float = 0
    error: str | None = None

    @property
    def ok(self):
        return self.status in {"covered", "confirmed_gap"}

    @property
    def identity(self):
        return (self.pair.instrument_id, self.pair.instrument_type, self.pair.timeframe,
                self.source_id, self.snapshot_generation, self.role, self.boundary)

    def message(self):
        windows = f"[{self.left_window[0]}, {self.left_window[1]})"
        if self.right_window is not None:
            windows += f" / [{self.right_window[0]}, {self.right_window[1]})"
        prefix = f"{self.pair}: граница {self.boundary}, ожидался бар {self.expected}; поиск {windows}"
        if self.status == "covered":
            return f"{prefix}: граница покрыта фактическим баром {self.last_before}"
        if self.status == "confirmed_gap":
            return (f"{prefix}: перерыв на границе подтверждён соседними барами "
                    f"{self.last_before} и {self.first_after}, разрыв {_span(timedelta(seconds=self.gap_seconds))}")
        return f"{prefix}: конец диапазона не покрыт — {self.error}"

    def payload(self):
        def stamp(value):
            return None if value is None else value.isoformat()+"Z"

        result = asdict(self)
        result["pair"] = {"instrument_id": self.pair.instrument_id, "instrument_type": self.pair.instrument_type,
                          "contract": self.pair.short_name or (self.pair.ticker if self.pair.instrument_type == "share" else "контракт не указан"),
                          "timeframe": self.pair.timeframe}
        for key in ("boundary", "expected", "last_before", "first_after"):
            result[key] = stamp(getattr(self, key))
        for key in ("left_window", "right_window"):
            value = getattr(self, key)
            result[key] = None if value is None else [stamp(item) for item in value]
        return result


def _boundary_frame(frame, lower, upper, pair):
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["datetime"])
    frame = frame.copy()
    for column, expected in (("instrument_id", pair.instrument_id), ("timeframe", pair.timeframe)):
        if column in frame and expected is not None and not frame[column].eq(expected).all():
            raise ValueError(f"данные другой пары: {column}")
    frame["datetime"] = pd.to_datetime(frame["datetime"], utc=True, format="mixed").dt.tz_convert(None)
    if "is_complete" in frame:
        frame = frame.loc[frame["is_complete"].astype(bool)]
    return frame.loc[(frame["datetime"] >= lower) & (frame["datetime"] < upper)].sort_values(
        "datetime").drop_duplicates("datetime", keep="last").reset_index(drop=True)


def check_right_boundary(pair, *, start, boundary, load_candles, client_provider=None, clock=None,
                         deadline=None, role="end", initial_tail=None):
    start, boundary = to_naive(start).to_pydatetime(), to_naive(boundary).to_pydatetime()
    lower = max(start, boundary-LOOKAHEAD)
    upper = min(boundary+LOOKAHEAD, to_naive(deadline).to_pydatetime()) if deadline is not None else boundary+LOOKAHEAD
    expected = CandleScheduler(pair.timeframe).previous_candle_start(boundary)
    source, snapshot = source_identity(client_provider)
    result = BoundaryCheck(pair, source, snapshot, role, boundary, expected, (lower, boundary))

    def fetch(left, right):
        frame, uid = load_candles(ticker=pair.ticker, instrument_type=pair.instrument_type, timeframe=pair.timeframe,
            start_date=left, end_date=right, instrument_id=pair.instrument_id, client_provider=client_provider, clock=clock)
        if pair.instrument_id is not None and uid != pair.instrument_id:
            raise ValueError("источник вернул другую идентичность инструмента")
        return _boundary_frame(frame, left, right, pair)

    try:
        if initial_tail is not None:
            frame, initial_lower = initial_tail
            result = replace(result, left_window=(initial_lower, boundary))
            tail = _boundary_frame(frame, initial_lower, boundary, pair)
        else:
            tail = fetch(lower, boundary)
        if not tail.empty and tail["datetime"].max() >= expected:
            return replace(result, status="covered", last_before=tail["datetime"].max().to_pydatetime())
        if initial_tail is not None and initial_lower > lower:
            if not tail.empty:
                result = replace(result, last_before=tail["datetime"].max().to_pydatetime())
            result = replace(result, left_window=(lower, boundary))
            tail = fetch(lower, boundary)
        if tail.empty:
            return replace(result, error="не покрывают конец диапазона: пустой рабочий хвост")
        last = tail["datetime"].max().to_pydatetime()
        if last >= expected:
            return replace(result, status="covered", last_before=last)
        result = replace(result, last_before=last, right_window=(boundary, upper))
        if not pair.instrument_id:
            return replace(result, error="не разрешена устойчивая идентичность пары")
        if upper <= boundary:
            return replace(result, error="нет допустимого окна продолжения до границы обращения")
        right = fetch(boundary, upper)
        if right.empty:
            return replace(result, error="нет допустимого бара-продолжения")
        following = right["datetime"].min().to_pydatetime()
        gap = max(timedelta(0), following-(last+_period(pair.timeframe)))
        result = replace(result, first_after=following, gap_seconds=gap.total_seconds())
        if gap > LOOKAHEAD:
            return replace(result, error="разрыв целиком превышает семь суток")
        return replace(result, status="confirmed_gap")
    except Exception as exc:
        return replace(result, error=f"ошибка обязательного диагностического чтения: {exc}")


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
            family = getattr(instrument, "family", None)
            if isinstance(family, str):
                from src.instruments.futures import FAMILIES
                assignments = future_strategies.get(family, future_strategies.get(FAMILIES[family][0], []))
            else:
                assignments = future_strategies.get(instrument.ticker[:2].upper(), [])
        else:
            assignments = share_strategies.get(instrument.ticker, [])
        timeframes = {assignment.timeframe for assignment in assignments}
        timeframes.add(protection_timeframe)
        for timeframe in sorted(timeframes, key=lambda tf: _period(tf)):
            uid, name = getattr(instrument, "uid", None), getattr(instrument, "short_name", None)
            pairs.append(Pair(instrument.ticker, instrument.instrument_type, timeframe,
                              uid if isinstance(uid, str) else None, name if isinstance(name, str) else None))
    return tuple(pairs)


@dataclass(frozen=True)
class PreflightReport:
    """Итог проверки: список проблем и понятное пользователю сообщение."""

    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    start: datetime | None = None
    shift_to: datetime | None = None
    checked_pairs: int = 0
    detail: list[str] = field(default_factory=list)
    boundaries: tuple[BoundaryCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def message(self) -> str:
        if self.ok:
            lines = [f"Проверка пройдена: пар {self.checked_pairs}."]
        else:
            lines = [
                "Прогон невозможен — данные не прошли предварительную проверку:",
                *(f"  • {problem}" for problem in self.problems),
            ]
        if self.notes:
            if lines:
                lines.append("")
            lines.extend(f"  {note}" for note in self.notes)
        if self.detail:
            lines.append("")
            lines.append("Проверенные пары:")
            lines.extend(f"  - {line}" for line in self.detail)
        lines.extend(f"  {item.message()}" for item in self.boundaries if item.status == "confirmed_gap")
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
    coverage=None,
) -> PreflightReport:
    """Проверяет источник, разрешение инструментов и покрытие диапазона.

    ``load_candles`` — загрузчик свечей прогона: подставляется тот же, что
    использует кэш, поэтому проверка говорит ровно о тех данных, которые увидит
    прогон. ``client_provider`` и ``clock`` уходят в загрузчик так же, как в
    кэше, иначе проверка считала бы границы от «сейчас». ``ping`` —
    необязательная проверка живости источника. ``coverage`` — необязательный
    необязательный счётный запрос покрытия к источнику. Его отсутствие не влияет
    на обязательные проверки.

    Начало в нерабочей точке календаря — один повтор проверки: если предложенный
    сдвиг даёт покрытый прогрев, он возвращается в ``start``, а ``notes`` объясняет
    сдвиг. Если после сдвига осталась проблема (обычно — конец диапазона), она и
    попадает в отчёт: чинить пользователю остаётся только её.
    """
    start_dt = to_naive(start).to_pydatetime()
    end_dt = to_naive(end).to_pydatetime()
    if end_dt <= start_dt:
        return PreflightReport(problems=("конец диапазона должен быть позже начала",))
    if not pairs:
        return PreflightReport(problems=("не выбрано ни одной активной пары инструментов",))

    ping_problem = None
    if ping is not None:
        try:
            ping()
        except Exception as exc:  # noqa: BLE001 — причина уходит в сообщение пользователю
            ping_problem = f"источник данных недоступен: {exc}"

    report = _check_all(
        pairs,
        start_dt=start_dt,
        end_dt=end_dt,
        warmup_bars=warmup_bars,
        load_candles=load_candles,
        client_provider=client_provider,
        clock=clock,
        ping_problem=ping_problem,
    )
    shift_to = report.shift_to
    # Недоступный источник сдвигом не лечится: второй проход по нему ничего
    # не добавит, а живость источника из первого отчёта потерялась бы.
    if not report.ok and shift_to is not None and ping_problem is None and start_dt < shift_to < end_dt:
        shifted = _check_all(
            pairs,
            start_dt=shift_to,
            end_dt=end_dt,
            warmup_bars=warmup_bars,
            load_candles=load_candles,
            client_provider=client_provider,
            clock=clock,
            ping_problem=None,
        )
        note = (
            f"начало диапазона сдвинуто с {start_dt:%Y-%m-%d %H:%M} на {shift_to:%Y-%m-%d %H:%M}: "
            f"раньше свечей не было (праздник, выходной или начало истории данных)"
        )
        report = PreflightReport(
            problems=shifted.problems,
            notes=shifted.notes + (note,),
            start=shift_to,
            shift_to=None,
            checked_pairs=shifted.checked_pairs,
            detail=shifted.detail,
            boundaries=shifted.boundaries,
        )
    elif not report.ok:
        return report

    return _with_coverage_note(
        report,
        pairs,
        start_dt=report.start or start_dt,
        end_dt=end_dt,
        coverage=coverage,
    )


def _with_coverage_note(
    report: PreflightReport,
    pairs,
    *,
    start_dt: datetime,
    end_dt: datetime,
    coverage=None,
) -> PreflightReport:
    """Дополняет отчёт необязательными сведениями о наблюдаемом покрытии."""
    if coverage is None:
        return report
    resolved = [
        replace(pair, instrument_id=next((item.pair.instrument_id for item in report.boundaries
            if (item.pair.ticker, item.pair.instrument_type, item.pair.timeframe) ==
               (pair.ticker, pair.instrument_type, pair.timeframe)), pair.instrument_id))
        for pair in pairs
    ]
    try:
        note = _coverage_note(resolved, start_dt=start_dt, end_dt=end_dt, coverage=coverage)
    except Exception as exc:  # noqa: BLE001 — сведения не должны ломать прогон
        log.warning("Сведения о покрытии диапазона не получены: %s", exc)
        note = f"сведения о покрытии диапазона не получены: {exc}"
    if note is None:
        return report
    return PreflightReport(
        problems=report.problems,
        notes=report.notes + (note,),
        start=report.start,
        shift_to=report.shift_to,
        checked_pairs=report.checked_pairs,
        detail=report.detail,
        boundaries=report.boundaries,
    )


def _coverage_note(
    pairs,
    *,
    start_dt: datetime,
    end_dt: datetime,
    coverage,
) -> str | None:
    """Сведения о покрытии по счётному запросу без вывода о целостности."""
    driver = smallest_period(pair.timeframe for pair in pairs)
    scanned = [pair for pair in pairs if _period(pair.timeframe) == driver]
    if not scanned:
        return None

    total = 0
    first_seen: str | None = None
    last_seen: str | None = None
    failed: list[str] = []
    for pair in scanned:
        if not pair.instrument_id:
            failed.append(f"{pair} (не разрешена идентичность)")
            continue
        try:
            data = coverage(instrument_id=pair.instrument_id, timeframe=pair.timeframe,
                            start_date=start_dt, end_date=end_dt)
        except Exception as exc:  # noqa: BLE001 — неполная отметка попадёт в сообщение
            failed.append(f"{pair} ({exc})")
            continue
        total += int(data.get("count") or 0)
        first, last = data.get("from"), data.get("to")
        if isinstance(first, str) and (first_seen is None or first < first_seen):
            first_seen = first
        if isinstance(last, str) and (last_seen is None or last > last_seen):
            last_seen = last

    if total:
        span = f", данные {first_seen} … {last_seen}" if first_seen and last_seen else ""
        found = f"свечей {total}{span}"
    else:
        found = "свечей нет"
    note = (
        f"таймфрейм {scanned[0].timeframe}, покрытие диапазона "
        f"{start_dt:%Y-%m-%d %H:%M} — {end_dt:%Y-%m-%d %H:%M}: {found}; "
        "внутренняя целостность не проверена"
    )
    if failed:
        note += f"; не проверено: {', '.join(failed)}"
    return note


def _check_all(
    pairs,
    *,
    start_dt: datetime,
    end_dt: datetime,
    warmup_bars: int,
    load_candles,
    client_provider,
    clock,
    ping_problem: str | None = None,
) -> PreflightReport:
    """Один проход по всем парам: границы диапазона и отметка о недоступном источнике."""
    problems: list[str] = []
    detail: list[str] = []
    boundaries = []
    if ping_problem is not None:
        problems.append(ping_problem)

    driver_period = min((_period(pair.timeframe) for pair in pairs), default=timedelta(minutes=1))
    driver_pairs = [pair for pair in pairs if _period(pair.timeframe) == driver_period]
    shift_candidates: list[datetime] = []
    for pair in pairs:
        line, pair_problems, shift_to = _check_pair(
            pair,
            start_dt=start_dt,
            end_dt=end_dt,
            warmup_bars=warmup_bars,
            load_candles=load_candles,
            client_provider=client_provider,
            clock=clock,
            boundary_results=boundaries,
            is_driver=_period(pair.timeframe) == driver_period,
        )
        detail.append(line)
        problems.extend(pair_problems)
        if shift_to is not None:
            shift_candidates.append(shift_to)

    # Сдвиг выравнивается по драйверному потоку, а не по самому старшему
    # таймфрейму: старший не должен переносить начало на свой следующий бар.
    shift_to = _align_up(max(shift_candidates), driver_pairs) if shift_candidates else None
    return PreflightReport(
        problems=tuple(problems),
        start=start_dt,
        shift_to=shift_to,
        checked_pairs=len(pairs),
        detail=detail,
        boundaries=tuple(boundaries),
    )


def _align_up(moment: datetime, pairs) -> datetime | None:
    """Округляет момент вверх до сетки переданных пар.

    Для сдвига начала передаётся драйверный (наименьший активный) таймфрейм:
    начало должно совпасть с баром драйверного потока, а не со старшим баром.
    Округление вверх (а не вниз) не заводит начало раньше доступного бара.
    """
    grid = max((_period(pair.timeframe) for pair in pairs), default=timedelta(minutes=1))
    seconds = int(grid.total_seconds())
    elapsed = int((to_naive(moment).to_pydatetime() - _EPOCH).total_seconds())
    return _EPOCH + timedelta(seconds=-(-elapsed // seconds) * seconds)


def _check_pair(
    pair: Pair,
    *,
    start_dt: datetime,
    end_dt: datetime,
    warmup_bars: int,
    load_candles,
    client_provider,
    clock,
    boundary_results=None,
    is_driver: bool = True,
) -> tuple[str, list[str], datetime | None]:
    """Проверяет одну пару и возвращает строку отчёта, проблемы и возможный сдвиг.

    Хвост сверяется с ``конец − период`` пары: у 1h свой бар, а не минута конца,
    поэтому вечерняя пауза в старшем таймфрейме не отвергает прогон.

    ``is_driver`` отмечает наименьший активный таймфрейм: только он задаёт
    начало диапазона и может предложить сдвиг. Старший таймфрейм берёт контекст
    из закрытого прогрева до начала, поэтому бар, открывшийся до начала, его не
    переносит.
    """
    period = _period(pair.timeframe)
    warmup_span = max(1, warmup_bars) * period
    head_from = start_dt - warmup_span
    try:
        head, uid = load_candles(
            ticker=pair.ticker,
            instrument_type=pair.instrument_type,
            timeframe=pair.timeframe,
            start_date=head_from,
            end_date=start_dt + period,
            instrument_id=pair.instrument_id,
            client_provider=client_provider,
            clock=clock,
        )
        if pair.instrument_id is not None and uid != pair.instrument_id:
            raise ValueError("источник вернул другую идентичность инструмента")
        if isinstance(uid, str) and uid:
            pair = replace(pair, instrument_id=uid)
    except Exception as exc:
        return f"{pair}: загрузка не удалась ({exc})", [
            f"{pair}: инструмент не разрешается или данные недоступны ({exc})"
        ], None

    problems: list[str] = []
    try:
        first_seen, head_last = _frame_bounds(head)
    except LookupError:
        # Сдвиг начала — только по драйверному потоку: он задаёт шаг рыночного
        # времени. Старший таймфрейм без свечей в начале не тянет за собой весь
        # прогон, если прогрев и конец диапазона покрыты.
        ahead = _nearest_after(pair, start_dt, load_candles, client_provider, clock) if is_driver else None
        hint = f" — ближайшая свеча {ahead:%Y-%m-%d %H:%M}" if ahead else ""
        return f"{pair}: нет свечей в начале диапазона{hint}", [
            f"{pair}: свечи не покрывают начало диапазона {start_dt:%Y-%m-%d %H:%M}{hint}"
        ], ahead

    if is_driver and head_last < start_dt:
        # Начало задаёт драйверный поток: переносим его ровно на ближайший
        # доступный бар — так повторная проверка не сдвигает начало снова.
        ahead = _nearest_after(pair, start_dt, load_candles, client_provider, clock)
        hint = f" — ближайшая свеча {ahead:%Y-%m-%d %H:%M}" if ahead else ""
        return f"{pair}: нет бара, открывшегося в {start_dt:%Y-%m-%d %H:%M}{hint}", [
            f"{pair}: нет бара, открывшегося в {start_dt:%Y-%m-%d %H:%M} — начало диапазона не покрыто{hint}"
        ], ahead
    if first_seen > head_from + period:
        problems.append(
            f"{pair}: прогрев не покрыт — данные начинаются с {first_seen:%Y-%m-%d %H:%M}, "
            f"нужно с {head_from:%Y-%m-%d %H:%M} ({warmup_bars} бар(ов) {pair.timeframe})"
        )

    expected_last = CandleScheduler(pair.timeframe).previous_candle_start(end_dt)
    tail_from = max(expected_last-period, start_dt)
    try:
        tail, tail_uid = load_candles(ticker=pair.ticker, instrument_type=pair.instrument_type,
            timeframe=pair.timeframe, start_date=tail_from, end_date=end_dt,
            instrument_id=pair.instrument_id, client_provider=client_provider, clock=clock)
        if pair.instrument_id is not None and tail_uid != pair.instrument_id:
            raise ValueError("источник вернул другую идентичность инструмента")
        check = check_right_boundary(pair, start=start_dt, boundary=end_dt, load_candles=load_candles,
            client_provider=client_provider, clock=clock, initial_tail=(tail, tail_from))
    except Exception as exc:
        check = BoundaryCheck(pair, *source_identity(client_provider), "end", end_dt, expected_last,
                              (tail_from, end_dt), error=f"ошибка обязательного чтения: {exc}")
    if boundary_results is not None:
        boundary_results.append(check)
    if not check.ok:
        behind = _nearest_before(pair, expected_last, load_candles, client_provider, clock)
        hint = f" — ближайшая свеча {behind:%Y-%m-%d %H:%M}" if behind else ""
        problems.append(check.message()+hint)
    return f"{pair}: {first_seen:%Y-%m-%d %H:%M} … {check.last_before or 'хвост не подтверждён'}", problems, None


def _nearest_after(pair: Pair, moment: datetime, load_candles, client_provider, clock):
    """Ближайшая свеча пары не раньше ``moment`` — для подсказки и сдвига начала."""
    try:
        frame, _ = load_candles(
            ticker=pair.ticker,
            instrument_type=pair.instrument_type,
            timeframe=pair.timeframe,
            start_date=moment,
            end_date=moment + LOOKAHEAD,
            instrument_id=None,
            client_provider=client_provider,
            clock=clock,
        )
    except Exception:  # noqa: BLE001 — подсказка не должна ломать проверку
        return None
    try:
        return _frame_bounds(frame)[0]
    except LookupError:
        return None


def _nearest_before(pair: Pair, moment: datetime, load_candles, client_provider, clock):
    """Ближайшая свеча пары не позже ``moment`` — подсказка про конец диапазона."""
    try:
        frame, _ = load_candles(
            ticker=pair.ticker,
            instrument_type=pair.instrument_type,
            timeframe=pair.timeframe,
            start_date=moment - LOOKAHEAD,
            end_date=moment,
            instrument_id=None,
            client_provider=client_provider,
            clock=clock,
        )
    except Exception:  # noqa: BLE001 — подсказка не должна ломать проверку
        return None
    try:
        return _frame_bounds(frame)[1]
    except LookupError:
        return None


def run_chain_preflight(selection, *, requirements, start, end, load_candles,
                        client_provider=None, clock=None, ping=None,
                        fixed_pairs=(), fixed_warmup=1, coverage=None):
    """Проверить назначенные участки, не требуя годы истории каждого выпуска."""
    problems, notes, detail = [], [], []
    boundaries = []
    checked = 0
    shifted = to_naive(start).to_pydatetime()
    if fixed_pairs:
        fixed = run_preflight(fixed_pairs, start=start, end=end, warmup_bars=fixed_warmup,
                              load_candles=load_candles, client_provider=client_provider,
                              clock=clock, ping=ping, coverage=coverage)
        problems.extend(fixed.problems)
        notes.extend(fixed.notes)
        detail.extend(fixed.detail)
        boundaries.extend(fixed.boundaries)
        checked += fixed.checked_pairs
        shifted = max(shifted, fixed.start or shifted)
    elif ping is not None:
        try:
            ping()
        except Exception as exc:
            problems.append(f"источник данных недоступен: {exc}")
    for chain in selection.chains:
        for segment in chain:
            card = segment.card

            def fetch(tf, lower, upper):
                frame, uid = load_candles(ticker=card.ticker, instrument_type="future", timeframe=tf,
                    start_date=lower, end_date=upper, instrument_id=card.uid,
                    client_provider=client_provider, clock=clock)
                if uid != card.uid:
                    raise ValueError("источник вернул другую идентичность инструмента")
                return _boundary_frame(frame, lower, upper, Pair(card.ticker, "future", tf, card.uid, card.short_name))

            required = requirements(card.family)
            driver_tf = min(required, key=_period) if required else None
            for tf, needed in required.items():
                checked += 1
                is_driver = tf == driver_tf
                period = _period(tf)
                label = f"{card.short_name or 'контракт не указан'} {tf}"
                try:
                    # Расширяем окно назад по числу реально закрытых баров.
                    span = max(LOOKAHEAD, needed*period)
                    while True:
                        lower = max(card.first_trade_date, segment.start-span)
                        head = fetch(tf, lower, min(segment.end, segment.start+LOOKAHEAD))
                        if head.empty:
                            raise LookupError("нет свечей в начале участка")
                        times = pd.to_datetime(head["datetime"])
                        prior = head.loc[times+period <= segment.start]
                        if len(prior) >= needed or lower == card.first_trade_date:
                            break
                        span *= 2
                    if len(prior) < needed:
                        raise LookupError(f"прогрев: нужно {needed} закрытых баров, имеется {len(prior)}")
                    trading = times[(times >= segment.start) & (times < segment.end)]
                    if is_driver and trading.empty:
                        raise LookupError("нет рабочего бара в начале участка")
                    first = to_naive(trading.min()).to_pydatetime() if not trading.empty else None
                    # Начало диапазона двигает только драйверный поток. Старший
                    # таймфрейм берёт контекст из прогрева и не переносит старт
                    # на свой следующий бар.
                    if is_driver and first is not None and segment.start == start and first > segment.start:
                        shifted = max(shifted, first)
                    if first is not None and first > segment.start:
                        notes.append(f"{label}: первый рабочий бар после границы {first:%Y-%m-%d %H:%M}")
                    check = check_right_boundary(Pair(card.ticker, "future", tf, card.uid, card.short_name),
                        start=segment.start, boundary=segment.end, load_candles=load_candles,
                        client_provider=client_provider, clock=clock, deadline=segment.deadline,
                        role="end" if segment.end == end else "cutoff")
                    boundaries.append(check)
                    if not check.ok:
                        raise LookupError(f"конец участка не покрыт: {check.message()}")
                    detail.append(f"{label}: участок {segment.start:%Y-%m-%d %H:%M} — {segment.end:%Y-%m-%d %H:%M}, прогрев {len(prior)}/{needed}")
                    if tf == "1m" and coverage is not None:
                        try:
                            data = coverage(instrument_id=card.uid, timeframe=tf,
                                            start_date=segment.start, end_date=segment.end)
                            notes.append(
                                f"{label}: наблюдаемое покрытие; свечей {int(data.get('count') or 0)}; "
                                "внутренняя целостность не проверена"
                            )
                        except Exception as exc:  # noqa: BLE001 — сведения не отменяют прогон
                            notes.append(f"{label}: сведения о покрытии не получены ({exc})")
                except Exception as exc:
                    problems.append(f"{label}: {exc}")
            if segment.end == segment.cutoff and segment.cutoff < end:
                try:
                    exit_end = min(segment.deadline, end)
                    # Команда создана на закрытии тика, не ранее календарной границы.
                    exit_from = _align_up(segment.cutoff, [Pair(card.ticker, "future", "1m")])
                    exits = fetch("1m", exit_from, exit_end) if exit_from < exit_end else pd.DataFrame()
                    if exits.empty:
                        raise LookupError("нет причинно допустимого минутного бара закрытия до deadline/конца")
                except Exception as exc:
                    problems.append(f"{card.short_name or 'контракт не указан'}: {exc}")
    if shifted > start:
        notes.append(f"начало диапазона сдвинуто с {start} на {shifted}: первая доступная рабочая свеча")
    return PreflightReport(problems=tuple(problems), notes=tuple(notes), start=shifted,
                           checked_pairs=checked, detail=detail, boundaries=tuple(boundaries))
