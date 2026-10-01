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


def _candles(windows):
    """Загрузчик свечей: по окну запроса отдаёт заранее заданный срез."""

    def load(ticker, instrument_type="share", timeframe="1h", start_date=None, end_date=None, **kw):
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

        assert calls == [HEAD_WINDOW, TAIL_WINDOW]

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
