from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.history.preflight import Pair, check_right_boundary, run_preflight


def moment(value):
    return datetime.fromisoformat(value)


class Source:
    def __init__(self, stamps, *, uid="ng-1", error=False, metadata=None):
        self.stamps = stamps
        self.uid = uid
        self.error = error
        self.metadata = metadata or {}
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.error and kw["start_date"] >= self.calls[0]["end_date"]:
            raise RuntimeError("diagnostic unavailable")
        # Включительная выдача намеренно проверяет нормализацию полуинтервала.
        times = pd.to_datetime(self.stamps, utc=True, format="mixed")
        left, right = pd.Timestamp(kw["start_date"], tz="UTC"), pd.Timestamp(kw["end_date"], tz="UTC")
        times = times[(times >= left) & (times <= right)]
        frame = pd.DataFrame({"datetime": times, "close": 100, **self.metadata})
        return frame, self.uid


@pytest.mark.parametrize("boundary,last,next_bar,name", [
    ("2025-01-27 00:00", "2025-01-24 20:49", "2025-01-27 07:00", "NG-1.25"),
    ("2025-02-24 00:00", "2025-02-21 20:49", "2025-02-24 07:00", "NG-2.25"),
    ("2025-03-15 12:00", "2025-03-14 20:49", "2025-03-17 07:00", "NG-3.25"),
    ("2025-03-14 23:00", "2025-03-14 20:49", "2025-03-15 07:00", "NG-3.25"),
])
@pytest.mark.parametrize("tf,delta", [("1m", 0), ("15m", 4), ("1h", 49), ("4h", 169), ("1d", 1249)])
def test_session_boundaries_use_independently_given_neighbors(boundary, last, next_bar, name, tf, delta):
    last = moment(last)-timedelta(minutes=delta)
    source = Source([last.isoformat(), next_bar])
    provider = SimpleNamespace(base_url="local-source", snapshot_generation=42)
    pair = Pair("NG", "future", tf, "ng-1", name)
    result = check_right_boundary(pair, start=moment(boundary)-timedelta(days=5),
        boundary=moment(boundary), deadline=moment(next_bar)+timedelta(days=1), load_candles=source,
        client_provider=provider)
    nominal = (boundary == "2025-03-15 12:00" and tf == "1d") or (
        boundary == "2025-03-14 23:00" and tf in {"4h", "1d"})
    assert result.status == ("covered" if nominal else "confirmed_gap")
    assert result.last_before == last
    assert result.first_after == (None if nominal else moment(next_bar))
    assert result.snapshot_generation == 42
    assert len(source.calls) == (1 if nominal else 2)
    assert all(call["instrument_id"] == "ng-1" and call["timeframe"] == tf for call in source.calls)
    assert name in result.message()
    assert ("покрыта фактическим баром" if nominal else "перерыв на границе подтверждён") in result.message()
    assert "close" not in result.payload()


def check(stamps, **kw):
    source = kw.pop("source", Source(stamps))
    result = check_right_boundary(Pair("NG", "future", "1m", "ng-1", "NG-1.25"),
        start=moment("2025-01-20 00:00"), boundary=moment("2025-01-27 00:00"), load_candles=source, **kw)
    return result, source


def test_nominal_tail_is_fast_and_diagnostic_boundary_bar_is_excluded():
    result, source = check(["2025-01-26 23:59", "2025-01-27 00:00"])
    assert result.status == "covered" and result.last_before == moment("2025-01-26 23:59")
    assert result.first_after is None and len(source.calls) == 1


def test_normalizes_timezone_order_and_duplicates():
    result, _ = check(["2025-01-27T10:00:00+03:00", "2025-01-24T23:49:00+03:00", "2025-01-24T20:49:00Z"])
    assert result.last_before == moment("2025-01-24 20:49")
    assert result.first_after == moment("2025-01-27 07:00")


@pytest.mark.parametrize("stamps,error", [
    (["2025-01-24 20:49"], "нет допустимого бара-продолжения"),
    (["2025-01-27 07:00"], "пустой рабочий хвост"),
    (["2025-01-20 12:00", "2025-02-02 07:00"], "разрыв целиком"),
])
def test_unconfirmed_boundaries_fail(stamps, error):
    result, _ = check(stamps)
    assert not result.ok and error in result.message()


@pytest.mark.parametrize("deadline", ["2025-01-27 07:00", "2025-01-27 06:59"])
def test_continuation_at_or_after_deadline_is_not_admissible(deadline):
    result, source = check(["2025-01-24 20:49", "2025-01-27 07:00"], deadline=moment(deadline))
    assert not result.ok
    assert source.calls[-1]["end_date"] == moment(deadline)


@pytest.mark.parametrize("metadata", [{"instrument_id": "other"}, {"timeframe": "1h"}])
def test_response_of_another_pair_cannot_confirm_tail(metadata):
    result, _ = check([], source=Source(["2025-01-24 20:49", "2025-01-27 07:00"], metadata=metadata))
    assert not result.ok and "другой пары" in result.message()


def test_other_uid_and_diagnostic_errors_fail_with_search_windows():
    for source in (Source(["2025-01-24 20:49"], uid="other"),
                   Source(["2025-01-24 20:49", "2025-01-27 07:00"], error=True)):
        result, _ = check([], source=source)
        assert not result.ok and "ошибка" in result.message()
        assert "2025-01-27 00:00" in result.message() and "поиск [" in result.message()


def test_fixed_preflight_keeps_resolved_uid_on_diagnostic_requests():
    start, end = moment("2025-01-24 20:48"), moment("2025-01-25 12:00")
    source = Source(["2025-01-24 20:46", "2025-01-24 20:47", "2025-01-24 20:48", "2025-01-24 20:49", "2025-01-27 07:00"])
    report = run_preflight([Pair("NG", "future", "1m", short_name="NG-1.25")], start=start, end=end,
                          warmup_bars=2, load_candles=source)
    assert report.ok and report.boundaries[0].status == "confirmed_gap"
    assert report.boundaries[0].pair.instrument_id == "ng-1"
    assert all(call["instrument_id"] == "ng-1" for call in source.calls if call["start_date"] >= end)
