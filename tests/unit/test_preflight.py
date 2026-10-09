from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.history.preflight import (
    Pair,
    active_pairs,
    run_preflight,
    smallest_period,
)

START = datetime(2024, 1, 1, 0, 0)
END = datetime(2024, 1, 2, 0, 0)
WARMUP = 2


def _frame(first: str, last: str, step_minutes: int = 1) -> pd.DataFrame:
    stamps = pd.date_range(first, last, freq=f"{step_minutes}min")
    return pd.DataFrame(
        {
            "datetime": stamps,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1,
        }
    )


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.Series([], dtype="datetime64[ns]"),
            "open": [], "high": [], "low": [], "close": [], "volume": [],
        }
    )


HEAD_WINDOW = (pd.Timestamp("2023-12-31 23:58"), pd.Timestamp("2024-01-01 00:01"))
TAIL_WINDOW = (pd.Timestamp("2024-01-01 23:58"), pd.Timestamp("2024-01-02 00:00"))
SCAN_WINDOW = (pd.Timestamp(START), pd.Timestamp(END))


def _candles(windows, scan_windows=(SCAN_WINDOW,), scan=None, seen=None):
    """Загрузчик свечей: по окну запроса отдаёт заранее заданный срез.

    Окна сканирования разрывов (от начала диапазона до его конца) в списке не
    значатся: проверка границ от них не зависит, поэтому по умолчанию они
    отдаются пустым срезом — без разрывов. Для проверки самого сканирования срез
    передаётся в ``scan``, а ``seen`` собирает таймфреймы запросов.
    """
    windows = dict(windows)
    for window in scan_windows:
        windows.setdefault(window, scan if scan is not None else _empty())

    def load(ticker, instrument_type="share", timeframe="1m", start_date=None, end_date=None, **kw):
        if seen is not None:
            seen.append((timeframe, start_date, end_date))
        probe = (pd.Timestamp(start_date), pd.Timestamp(end_date))
        if probe not in windows:
            raise AssertionError(f"Незапланированный запрос: {start_date} … {end_date}")
        return windows[probe], f"{ticker}-uid"

    return load


def _head(first="2023-12-31 23:58:00", last="2024-01-01 00:00:00"):
    return _frame(first, last)


def _tail(first="2024-01-01 23:59:00", last="2024-01-01 23:59:00"):
    return _frame(first, last)


HEAD_WINDOW = (pd.Timestamp("2023-12-31 23:58"), pd.Timestamp("2024-01-01 00:01"))
TAIL_WINDOW = (pd.Timestamp("2024-01-01 23:58"), pd.Timestamp("2024-01-02 00:00"))
SCAN_WINDOW = (pd.Timestamp(START), pd.Timestamp(END))


def _full_source(head=None, tail=None):
    return _candles(
        {
            HEAD_WINDOW: _head() if head is None else head,
            TAIL_WINDOW: _tail() if tail is None else tail,
        }
    )


PAIRS = (Pair("SBER", "share", "1m"),)


def _run(pairs=PAIRS, **kw):
    return run_preflight(
        pairs,
        start=kw.pop("start", START),
        end=kw.pop("end", END),
        warmup_bars=kw.pop("warmup_bars", WARMUP),
        load_candles=kw.pop("load_candles", _full_source()),
        **kw,
    )


class TestSmallestPeriod:
    def test_step_is_min_period(self):
        assert smallest_period(["1m", "5m", "1h"]) == timedelta(minutes=1)
        assert smallest_period(["5m", "1h"]) == timedelta(minutes=5)

    def test_defaults_to_one_minute(self):
        assert smallest_period([]) == timedelta(minutes=1)

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError):
            smallest_period(["3m"])


class TestActivePairs:
    def _instrument(self, ticker="SBER", kind="share"):
        return MagicMock(ticker=ticker, instrument_type=kind)

    def test_timeframes_from_assignments_plus_protection(self):
        assignments = {MagicMock(timeframe="1h"), MagicMock(timeframe="5m")}

        pairs = active_pairs(
            [self._instrument()], {"SBER": list(assignments)}, {}, protection_timeframe="1m"
        )

        assert [pair.timeframe for pair in pairs] == ["1m", "5m", "1h"]
        assert all(pair.ticker == "SBER" for pair in pairs)

    def test_future_instrument_keyed_by_prefix(self):
        pairs = active_pairs(
            [self._instrument("NGZ6", "future")],
            {},
            {"NG": [MagicMock(timeframe="1h")]},
            protection_timeframe="1m",
        )

        assert [(pair.ticker, pair.instrument_type, pair.timeframe) for pair in pairs] == [
            ("NGZ6", "future", "1m"),
            ("NGZ6", "future", "1h"),
        ]

    def test_protection_timeframe_included_without_assignments(self):
        pairs = active_pairs([self._instrument()], {}, {})

        assert [pair.timeframe for pair in pairs] == ["1m"]


class TestSuccessfulPreflight:
    def test_covered_range_passes(self):
        report = _run()

        assert report.ok is True
        assert report.problems == ()
        assert report.checked_pairs == 1
        assert "Проверка пройдена: пар 1" in report.message()

    def test_probes_touch_range_boundaries(self):
        source = _full_source()
        calls = []

        def load(**kw):
            calls.append((pd.Timestamp(kw["start_date"]), pd.Timestamp(kw["end_date"])))
            return source(**kw)

        _run(load_candles=load)

        # границы диапазона плюс скан разрывов по всему диапазону
        assert calls == [HEAD_WINDOW, TAIL_WINDOW, SCAN_WINDOW]

    def test_passes_source_and_clock_to_loader(self):
        captured = {}

        def load(**kw):
            captured.setdefault("source", (kw.get("client_provider"), kw.get("clock")))
            return _full_source()(**kw)

        provider, clock = object(), object()
        _run(load_candles=load, client_provider=provider, clock=clock)

        assert captured["source"] == (provider, clock)

    def test_ping_checked(self):
        ping = MagicMock()

        assert _run(ping=ping).ok is True
        ping.assert_called_once_with()


class TestSourceProblems:
    def test_unreachable_source_blocks_run(self):
        report = _run(ping=MagicMock(side_effect=ConnectionRefusedError("127.0.0.1:8100")))

        assert report.ok is False
        assert "источник данных недоступен" in report.problems[0]
        assert "Прогон невозможен" in report.message()

    def test_unresolvable_instrument_blocks_run(self):
        def load(**kw):
            raise ValueError("Инструмент 'SBER' не найден или недоступен")

        report = _run(load_candles=load)

        assert report.ok is False
        assert "SBER 1m" in report.problems[0]
        assert "не разрешается" in report.problems[0]

    def test_message_lists_problem_pairs(self):
        report = _run(load_candles=MagicMock(side_effect=ValueError("нет данных")))

        assert "SBER 1m: загрузка не удалась (нет данных)" in report.detail


class TestCoverageProblems:
    def test_no_candles_at_range_start(self):
        source = _candles({HEAD_WINDOW: pd.DataFrame(), TAIL_WINDOW: _tail()})

        report = _run(load_candles=source)

        assert report.ok is False
        assert "не покрывают начало диапазона 2024-01-01 00:00" in report.problems[0]

    def test_warmup_not_covered(self):
        report = _run(load_candles=_full_source(head=_head(first="2024-01-01 00:00:00")))

        assert report.ok is False
        assert any("прогрев не покрыт" in problem for problem in report.problems)

    def test_range_not_covered_at_end(self):
        report = _run(load_candles=_full_source(tail=_frame("2024-01-01 23:58", "2024-01-01 23:58")))

        assert report.ok is False
        assert any("конец диапазона не покрыт" in problem for problem in report.problems)

    def test_no_candles_at_range_end(self):
        report = _run(load_candles=_full_source(tail=pd.DataFrame()))

        assert report.ok is False
        assert any("не покрывают конец диапазона" in problem for problem in report.problems)

    def test_every_pair_reported(self):
        pairs = (Pair("SBER", "share", "1m"), Pair("GAZP", "share", "1m"))

        report = _run(pairs=pairs, load_candles=MagicMock(side_effect=ValueError("нет")))

        assert len(report.problems) == 2
        assert report.checked_pairs == 2

    def test_problem_message_shows_checked_pairs(self):
        report = _run(load_candles=MagicMock(side_effect=ValueError("нет")))

        assert "Проверенные пары:" in report.message()


class TestRangeValidation:
    def test_inverted_range_rejected(self):
        report = _run(start=END, end=START)

        assert report.ok is False
        assert report.problems == ("конец диапазона должен быть позже начала",)

    def test_empty_range_rejected(self):
        report = _run(start=START, end=START)

        assert report.ok is False

    def test_no_pairs_rejected(self):
        report = _run(pairs=())

        assert report.ok is False
        assert "ни одной активной пары" in report.problems[0]

    def test_step_follows_smallest_timeframe(self):
        report = run_preflight(
            (Pair("SBER", "share", "5m"),),
            start=START,
            end=datetime(2024, 1, 1, 1, 0),
            warmup_bars=WARMUP,
            load_candles=_candles(
                {
                    (pd.Timestamp("2023-12-31 23:50"), pd.Timestamp("2024-01-01 00:05")): _frame(
                        "2023-12-31 23:50", "2024-01-01 00:00", step_minutes=5
                    ),
                    (pd.Timestamp("2024-01-01 00:50"), pd.Timestamp("2024-01-01 01:00")): _frame(
                        "2024-01-01 00:50", "2024-01-01 01:00", step_minutes=5
                    ),
                }
            ),
        )

        assert report.ok is True


def _bars(*stamps: str) -> pd.DataFrame:
    """Кадр из перечисленных баров — точнее, чем равномерная сетка."""
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(list(stamps)),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1,
        }
    )


def _window(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp(start), pd.Timestamp(end)


def _hourly(*stamps: str) -> pd.DataFrame:
    return _bars(*stamps)


class TestHigherTimeframeTail:
    """Хвост старшего ТФ сверяется с его баром, а не с минутой конца диапазона."""

    PAIRS = (Pair("SBER", "share", "1h"),)
    HEAD = _window("2023-12-31 22:00", "2024-01-01 01:00")
    TAIL = _window("2024-01-01 22:00", "2024-01-02 00:00")
    NEAREST = _window("2023-12-25 23:00", "2024-01-01 23:00")
    HEAD_BARS = _hourly("2023-12-31 22:00:00", "2024-01-01 00:00:00")

    def _source(self, tail: pd.DataFrame, nearest: pd.DataFrame | None = None) -> _candles:
        windows = {self.HEAD: self.HEAD_BARS, self.TAIL: tail}
        if nearest is not None:
            windows[self.NEAREST] = nearest
        return _candles(windows)

    def test_last_hour_bar_closes_the_range(self):
        source = self._source(_hourly("2024-01-01 22:00:00", "2024-01-01 23:00:00"))

        report = _run(pairs=self.PAIRS, load_candles=source)

        assert report.ok is True
        assert report.start == START

    def test_tail_two_bars_older_than_end_is_reported(self):
        source = self._source(_hourly("2024-01-01 22:00:00"))

        report = _run(pairs=self.PAIRS, load_candles=source)

        assert report.ok is False
        assert "2024-01-01 23:00" in report.problems[0]
        assert "конец диапазона не покрыт" in report.problems[0]

    def test_empty_tail_names_nearest_candle(self):
        source = self._source(
            pd.DataFrame(), _hourly("2023-12-29 19:00:00", "2023-12-29 20:00:00")
        )

        report = _run(pairs=self.PAIRS, load_candles=source)

        assert report.ok is False
        assert "ближайшая свеча 2023-12-29 20:00" in report.problems[0]


class TestStartShift:
    """Начало в нерабочей точке календара переносится на первую доступную свечу."""

    END = datetime(2024, 1, 10, 0, 0)
    AHEAD = _window("2024-01-01 00:00", "2024-01-08 00:00")
    SHIFTED_HEAD = _window("2024-01-03 04:00", "2024-01-03 04:03")
    SHIFTED_TAIL = _window("2024-01-09 23:58", "2024-01-10 00:00")
    SHIFTED_HEAD_BARS = _bars("2024-01-03 04:00:00", "2024-01-03 04:01:00", "2024-01-03 04:02:00")
    SHIFTED_TAIL_BARS = _bars("2024-01-09 23:58:00", "2024-01-09 23:59:00")

    def _source(self, *, ahead=None, shifted_head=None, scan=None) -> _candles:
        return _candles(
            {
                HEAD_WINDOW: pd.DataFrame(),
                self.AHEAD: pd.DataFrame() if ahead is None else ahead,
                self.SHIFTED_HEAD: (
                    self.SHIFTED_HEAD_BARS if shifted_head is None else shifted_head
                ),
                self.SHIFTED_TAIL: self.SHIFTED_TAIL_BARS,
            },
            scan_windows=(
                SCAN_WINDOW,
                (pd.Timestamp(START), pd.Timestamp(self.END)),
                (pd.Timestamp("2024-01-03 04:02"), pd.Timestamp(self.END)),
            ),
            scan=scan,
        )

    def test_start_moves_to_first_available_candle(self):
        source = self._source(ahead=_bars("2024-01-03 04:00:00"))

        report = _run(start=START, end=self.END, load_candles=source)

        assert report.ok is True
        assert report.start == datetime(2024, 1, 3, 4, 2)
        assert "сдвинуто с 2024-01-01 00:00 на 2024-01-03 04:02" in report.notes[0]

    def test_shift_note_printed_with_passed_check(self):
        source = self._source(ahead=_bars("2024-01-03 04:00:00"))

        message = _run(start=START, end=self.END, load_candles=source).message()

        assert "Проверка пройдена: пар 1." in message
        assert "начало диапазона сдвинуто" in message

    def test_shift_and_gap_warning_coexist(self):
        scan = _frame("2024-01-03 04:02", "2024-01-03 04:10").drop(index=3)
        source = self._source(
            ahead=_bars("2024-01-03 04:00:00"),
            scan=scan,
        )

        report = _run(start=START, end=self.END, load_candles=source)

        assert report.ok is True
        assert "сдвинуто с 2024-01-01 00:00 на 2024-01-03 04:02" in report.notes[0]
        assert "разрывов данных: 1" in report.notes[-1]

    def test_remaining_problem_reported_after_shift(self):
        source = self._source(ahead=_bars("2024-01-03 04:00:00"), shifted_head=pd.DataFrame())

        report = _run(start=START, end=self.END, load_candles=source)

        assert report.ok is False
        assert report.start == datetime(2024, 1, 3, 4, 2)
        assert "сдвинуто" in report.notes[0]
        assert "2024-01-03 04:02" in report.problems[0]

    def test_data_beyond_lookahead_stays_an_error(self):
        report = _run(start=START, end=self.END, load_candles=self._source())

        assert report.ok is False
        assert report.start == START
        assert "свечи не покрывают начало диапазона 2024-01-01 00:00" in report.problems[0]
        assert "ближайшая свеча" not in report.problems[0]

    def test_shift_aligned_to_coarsest_timeframe(self):
        pairs = (Pair("SBER", "share", "1m"), Pair("SBER", "share", "1h"))
        source = _candles(
            {
                HEAD_WINDOW: pd.DataFrame(),
                _window("2023-12-31 22:00", "2024-01-01 01:00"): _hourly(
                    "2023-12-31 22:00:00", "2024-01-01 00:00:00"
                ),
                self.AHEAD: _bars("2024-01-03 04:07:00"),
                _window("2024-01-03 03:00", "2024-01-03 06:00"): _hourly(
                    "2024-01-03 03:00:00", "2024-01-03 04:00:00", "2024-01-03 05:00:00"
                ),
                _window("2024-01-03 04:58", "2024-01-03 05:01"): _bars(
                    "2024-01-03 04:58:00", "2024-01-03 04:59:00", "2024-01-03 05:00:00"
                ),
                self.SHIFTED_TAIL: self.SHIFTED_TAIL_BARS,
                _window("2024-01-09 22:00", "2024-01-10 00:00"): _hourly(
                    "2024-01-09 22:00:00", "2024-01-09 23:00:00"
                ),
            }
        )

        report = _run(pairs=pairs, start=START, end=self.END, load_candles=source)

        assert report.ok is True
        assert report.start == datetime(2024, 1, 3, 5, 0)

    def test_unreachable_source_is_not_hidden_by_shift(self):
        source = self._source(ahead=_bars("2024-01-03 04:00:00"))

        def ping():
            raise RuntimeError("соединение отклонено")

        report = _run(start=START, end=self.END, load_candles=source, ping=ping)

        assert report.ok is False
        assert report.start == START
        assert report.problems[0] == "источник данных недоступен: соединение отклонено"

    def test_shift_ignored_when_it_would_pass_end(self):
        source = self._source(ahead=_bars("2024-01-01 01:30:00"))

        report = _run(start=START, end=datetime(2024, 1, 1, 1, 0), load_candles=source)

        assert report.ok is False
        assert report.start == START
        assert "сдвинуто" not in report.message()


class TestContinuityScan:
    """Разрывы внутри диапазона — предупреждение, а не запрет на прогон."""

    LONG_END = START + timedelta(minutes=200_010)

    @staticmethod
    def _source(scan=None, *, head=None, tail=None, seen=None,
                scan_windows=(SCAN_WINDOW,), start=START, end=END):
        period = timedelta(minutes=1)
        return _candles(
            {
                (pd.Timestamp(start) - period * WARMUP, pd.Timestamp(start) + period): (
                    _head() if head is None else head
                ),
                (pd.Timestamp(end) - 2 * period, pd.Timestamp(end)): (
                    _tail() if tail is None else tail
                ),
            },
            scan_windows=scan_windows,
            scan=scan,
            seen=seen,
        )

    def _minutes_with_hole(self, hole_minutes: int) -> pd.DataFrame:
        stamps = pd.date_range("2024-01-01 00:00", "2024-01-01 00:30", freq="1min")
        stamps = stamps.delete(range(10, 10 + hole_minutes))
        return _bars(*[s.strftime("%Y-%m-%d %H:%M:%S") for s in stamps])

    def test_gap_counted_in_warning(self):
        source = self._source(scan=self._minutes_with_hole(5))

        report = _run(load_candles=source)

        assert report.ok is True
        assert report.problems == ()
        note = report.notes[-1]
        assert "таймфрейм 1m" in note
        assert "разрывов данных: 1" in note
        assert "пропущено баров 5" in note
        assert "самый длинный 5 м" in note
        assert "прогон перейдёт их по часам" in note

    def test_several_gaps_are_summed(self):
        scan = _bars(
            *[s.strftime("%Y-%m-%d %H:%M:%S")
              for s in pd.date_range("2024-01-01 00:00", "2024-01-01 00:20", freq="1min").delete([5, 6])]
        )
        source = self._source(scan=scan)

        report = _run(load_candles=source)

        assert "разрывов данных: 1" in report.notes[-1]
        assert "пропущено баров 2" in report.notes[-1]

    def test_longest_gap_reported_in_hours(self):
        stamps = list(pd.date_range("2024-01-01 00:00", "2024-01-01 00:20", freq="1min"))
        hole = [s for s in pd.date_range("2024-01-01 00:06", "2024-01-01 00:08", freq="1min")]
        stamps = [s for s in stamps if s not in hole]
        scan = _bars(*[s.strftime("%Y-%m-%d %H:%M:%S") for s in stamps])
        source = self._source(scan=scan)

        report = _run(load_candles=source)

        assert "самый длинный 3 м" in report.notes[-1]

    def test_clean_range_says_no_gaps(self):
        report = _run(load_candles=self._source())

        assert report.ok is True
        assert "разрывов данных не найдено" in report.notes[-1]

    def test_only_smallest_timeframe_is_scanned(self):
        """Разрывы старших ТФ выводятся из мелких: сканируется только ведущий."""
        seen = []
        hour = timedelta(hours=1)
        source = _candles(
            {
                (pd.Timestamp(START) - 2 * timedelta(minutes=1), pd.Timestamp(START) + timedelta(minutes=1)): _head(),
                (pd.Timestamp(START) - 2 * hour, pd.Timestamp(START) + hour): _hourly(
                    "2023-12-31 23:00:00", "2024-01-01 00:00:00"
                ),
                (pd.Timestamp(END) - 2 * timedelta(minutes=1), pd.Timestamp(END)): _tail(),
                (pd.Timestamp(END) - 2 * hour, pd.Timestamp(END)): _hourly("2024-01-01 23:00:00"),
            },
            seen=seen,
        )
        pairs = (Pair("SBER", "share", "1m"), Pair("SBER", "share", "1h"))

        report = _run(pairs=pairs, load_candles=source)

        assert report.ok is True
        scans = [
            tf for tf, start, end in seen
            if (pd.Timestamp(start), pd.Timestamp(end)) == SCAN_WINDOW
        ]
        assert scans == ["1m"]

    def test_partial_scan_names_checked_window(self):
        long_tail = _frame("2024-05-18 21:28", "2024-05-18 21:29")
        source = self._source(
            end=self.LONG_END,
            tail=long_tail,
            scan_windows=((pd.Timestamp(START), pd.Timestamp(self.LONG_END)),),
        )

        report = _run(end=self.LONG_END, load_candles=source)

        assert report.ok is True
        note = report.notes[-1]
        assert "хвост не проверялся" in note
        assert "2024-01-01 00:00" in note
        assert "разрывов данных не найдено" in note

    def test_scan_failure_is_a_warning_not_a_problem(self):
        def load(**kw):
            if (pd.Timestamp(kw["start_date"]), pd.Timestamp(kw["end_date"])) == SCAN_WINDOW:
                raise RuntimeError("источник молчит")
            return _full_source()(**kw)

        report = _run(load_candles=load)

        assert report.ok is True
        assert "не проверено: SBER 1m (источник молчит)" in report.notes[-1]

    def test_range_without_enough_bars_is_reported_as_unchecked(self):
        source = self._source(scan=_bars("2024-01-01 00:00:00"))

        report = _run(load_candles=source)

        assert report.ok is True
        assert "не проверено: SBER 1m (в диапазоне меньше двух свечей)" in report.notes[-1]

    def test_pair_scan_failure_is_listed_in_note(self):
        pairs = (Pair("SBER", "share", "1m"), Pair("GAZP", "share", "1m"))
        scan = self._minutes_with_hole(0)

        def load(ticker, **kw):
            if ticker == "GAZP" and (pd.Timestamp(kw["start_date"]), pd.Timestamp(kw["end_date"])) == SCAN_WINDOW:
                raise RuntimeError("нет данных")
            return self._source(scan=scan)(ticker, **kw)

        report = _run(pairs=pairs, load_candles=load)

        assert report.ok is True
        assert "не проверено: GAZP 1m" in report.notes[-1]

    def test_no_scan_when_run_is_impossible(self):
        source = _candles({HEAD_WINDOW: pd.DataFrame(), TAIL_WINDOW: _tail()})

        report = _run(load_candles=source)

        assert report.ok is False
        assert not any("разрыв" in note for note in report.notes)
