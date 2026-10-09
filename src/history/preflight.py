"""Предварительная проверка исторического прогона.

До входа в цикл проверяет, что источник данных отвечает, инструменты
разрешаются, а свечи покрывают диапазон вместе с прогревом. Проблемы
перечисляются пользователю целиком: прогон не начинается, пока есть хотя бы
одна непрочитанная пара «инструмент, таймфрейм».

Границы проверяются по правилам самой пары: хвост должен дотягиваться до бара
``конец − период`` этой пары, а не до минуты конца, поэтому старший таймфрейм
не требует данных там, где их и не бывает. Начало в нерабочей точке календаря
(праздник, выходной) не отвергает прогон: если ближайшая свеча нашлась в
``LOOKAHEAD`` и сдвиг даёт покрытый прогрев, начало переносится на неё, а
сдвиг попадает в отчёт. Данных, начинающихся заметно позже, это не касается —
такое расхождение остаётся ошибкой, чтобы опечатка в диапазоне не прошла молча.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src.data.timeutil import to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)

LOOKAHEAD = timedelta(days=7)
"""Насколько далеко в обе стороны проверка ищет ближайшую свечу."""

CONTINUITY_SCAN_BUDGET = 200_000
"""Сколько баров мельчайшего активного ТФ проверка готова просмотреть на разрывы.

Сканирование не блокирует прогон и служит предупреждением, поэтому у него есть
потолок: длинный диапазон проверяется начиная от начала, а в сообщении прямо
называется проверенный участок — «разрывов не найдено» на непроверенном хвосте
было бы ложным утверждением.
"""

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


@dataclass(frozen=True)
class PreflightReport:
    """Итог проверки: список проблем и понятное пользователю сообщение."""

    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    start: datetime | None = None
    shift_to: datetime | None = None
    checked_pairs: int = 0
    detail: list[str] = field(default_factory=list)

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
        )
    elif not report.ok:
        return report

    # Сканирование разрывов идёт после решения о сдвиге: пользователь интересует
    # разрывы на самом деле прогоняемого диапазона, а не отвергнутого запроса.
    return _with_continuity_note(
        report,
        pairs,
        start_dt=report.start or start_dt,
        end_dt=end_dt,
        load_candles=load_candles,
        client_provider=client_provider,
        clock=clock,
    )


def _with_continuity_note(
    report: PreflightReport,
    pairs,
    *,
    start_dt: datetime,
    end_dt: datetime,
    load_candles,
    client_provider,
    clock,
) -> PreflightReport:
    """Дополняет отчёт предупреждением о разрывах внутри диапазона.

    Предупреждение, а не проблема: прогон через разрыв корректен, решение о
    запуске остаётся за пользователем. Ошибка сканирования тоже остаётся
    предупреждением — молча пропустить проверку нельзя, но и отменять из-за неё
    прогон незачем.
    """
    try:
        note = _scan_continuity(
            pairs,
            start_dt=start_dt,
            end_dt=end_dt,
            load_candles=load_candles,
            client_provider=client_provider,
            clock=clock,
        )
    except Exception as exc:  # noqa: BLE001 — сканирование не должно ломать проверку
        log.warning("Сканирование разрывов не выполнено: %s", exc)
        note = f"разрывы внутри диапазона не проверены: {exc}"
    if note is None:
        return report
    return PreflightReport(
        problems=report.problems,
        notes=report.notes + (note,),
        start=report.start,
        shift_to=report.shift_to,
        checked_pairs=report.checked_pairs,
        detail=report.detail,
    )


def _scan_continuity(
    pairs,
    *,
    start_dt: datetime,
    end_dt: datetime,
    load_candles,
    client_provider,
    clock,
) -> str | None:
    """Ищет разрывы в срезе мельчайшего активного ТФ по всему запрошенному диапазону.

    Проверяются все пары мельчайшего таймфрейма: разрыв одной пары — это тоже
    потеря данных, о которой должен знать пользователь. Старшие таймфреймы не
    сканируются: их разрывы выводятся из мелких и только повторяют их.
    """
    driver = smallest_period(pair.timeframe for pair in pairs)
    scanned = [pair for pair in pairs if _period(pair.timeframe) == driver]
    if not scanned:
        return None
    budget_span = CONTINUITY_SCAN_BUDGET * driver
    scan_end = min(end_dt, start_dt + budget_span)
    partial = scan_end < end_dt

    gaps = 0
    missed = 0
    longest = timedelta(0)
    failed: list[str] = []
    for pair in scanned:
        try:
            frame, _ = load_candles(
                ticker=pair.ticker,
                instrument_type=pair.instrument_type,
                timeframe=pair.timeframe,
                start_date=start_dt,
                end_date=scan_end,
                instrument_id=None,
                client_provider=client_provider,
                clock=clock,
            )
            stamps = pd.to_datetime(frame["datetime"]).drop_duplicates().sort_values()
        except Exception as exc:  # noqa: BLE001 — неполное сканирование попадёт в сообщение
            failed.append(f"{pair} ({exc})")
            continue
        if len(stamps) < 2:
            failed.append(f"{pair} (в диапазоне меньше двух свечей)")
            continue
        holes = stamps.diff().dropna()
        holes = holes[holes > driver]
        if holes.empty:
            continue
        gaps += int(holes.size)
        missed += int(((holes - driver) / driver).sum())
        longest = max(longest, holes.max() - driver)

    if gaps:
        found = (
            f"разрывов данных: {gaps} (пропущено баров {missed}, "
            f"самый длинный {_span(longest)}); прогон перейдёт их по часам"
        )
    else:
        found = "разрывов данных не найдено"
    scope = (
        f"проверен участок {start_dt:%Y-%m-%d %H:%M} — {scan_end:%Y-%m-%d %H:%M} "
        f"(лимит {CONTINUITY_SCAN_BUDGET} баров, хвост не проверялся): "
        if partial
        else f"проверен весь диапазон {start_dt:%Y-%m-%d %H:%M} — {scan_end:%Y-%m-%d %H:%M}: "
    )
    note = f"таймфрейм {scanned[0].timeframe}, {scope}{found}"
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
    if ping_problem is not None:
        problems.append(ping_problem)

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
        )
        detail.append(line)
        problems.extend(pair_problems)
        if shift_to is not None:
            shift_candidates.append(shift_to)

    shift_to = _align_up(max(shift_candidates), pairs) if shift_candidates else None
    return PreflightReport(
        problems=tuple(problems),
        start=start_dt,
        shift_to=shift_to,
        checked_pairs=len(pairs),
        detail=detail,
    )


def _align_up(moment: datetime, pairs) -> datetime | None:
    """Округляет предложенное начало вверх до сетки старшего таймфрейма.

    Сдвиг по самой мелкой паре может попасть внутрь бара старшей: тот тогда не
    увидит свечи в окне прогрева. Округление вверх (а не вниз) сохраняет и
    начало прогрева мелкой пары — там свечи появляются до любой границы её бара.
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
) -> tuple[str, list[str], datetime | None]:
    """Проверяет одну пару и возвращает строку отчёта, проблемы и возможный сдвиг.

    Хвост сверяется с ``конец − период`` пары: у 1h свой бар, а не минута конца,
    поэтому вечерняя пауза в старшем таймфрейме не отвергает прогон.
    """
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
        ], None

    problems: list[str] = []
    try:
        first_seen, head_last = _frame_bounds(head)
    except LookupError:
        ahead = _nearest_after(pair, start_dt, load_candles, client_provider, clock)
        hint = f" — ближайшая свеча {ahead:%Y-%m-%d %H:%M}" if ahead else ""
        # Сдвиг берёт не первую свечу, а первую плюс прогрев: иначе прогон
        # стартовал бы с пустой историей, ради которой проверка и стоит.
        shift_to = ahead + warmup_span if ahead is not None else None
        return f"{pair}: нет свечей в начале диапазона{hint}", [
            f"{pair}: свечи не покрывают начало диапазона {start_dt:%Y-%m-%d %H:%M}{hint}"
        ], shift_to

    if head_last < start_dt:
        problems.append(
            f"{pair}: нет бара, открывшегося в {start_dt:%Y-%m-%d %H:%M} — начало диапазона не покрыто"
        )
    if first_seen > head_from + period:
        problems.append(
            f"{pair}: прогрев не покрыт — данные начинаются с {first_seen:%Y-%m-%d %H:%M}, "
            f"нужно с {head_from:%Y-%m-%d %H:%M} ({warmup_bars} бар(ов) {pair.timeframe})"
        )

    expected_last = end_dt - period
    try:
        _, tail_last = _frame_bounds(
            load_candles(
                ticker=pair.ticker,
                instrument_type=pair.instrument_type,
                timeframe=pair.timeframe,
                start_date=max(expected_last - period, start_dt),
                end_date=end_dt,
                instrument_id=None,
                client_provider=client_provider,
                clock=clock,
            )[0]
        )
    except LookupError:
        behind = _nearest_before(pair, expected_last, load_candles, client_provider, clock)
        hint = f" — ближайшая свеча {behind:%Y-%m-%d %H:%M}" if behind else ""
        return f"{pair}: нет свечей в конце диапазона{hint}", [
            f"{pair}: свечи не покрывают конец диапазона {end_dt:%Y-%m-%d %H:%M} "
            f"(ожидался бар {expected_last:%Y-%m-%d %H:%M}){hint}"
        ], None

    if tail_last < expected_last:
        problems.append(
            f"{pair}: последний бар {tail_last:%Y-%m-%d %H:%M} раньше ожидаемого "
            f"{expected_last:%Y-%m-%d %H:%M} — конец диапазона не покрыт"
        )
    return f"{pair}: {first_seen:%Y-%m-%d %H:%M} … {tail_last:%Y-%m-%d %H:%M}", problems, None


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
