from datetime import datetime

import pandas as pd

from src.history.contract_chain import build_chain
from src.history.preflight import run_chain_preflight
from src.history.selection import HistorySelection
from src.instruments.futures import FuturesCard


def selection():
    cards = tuple(FuturesCard.from_row({
        "instrument_id": f"uid-{i}", "ticker": f"NG-{i}.26", "name": f"NG-{i}.26 Газ",
        "first_trade_date": "2026-01-01T00:00:00Z",
        "expiration_date": f"2026-07-0{i+3}T00:00:00Z",
        "min_price_increment": 1, "min_price_increment_amount": 1,
        "initial_margin_on_buy": 10, "initial_margin_on_sell": 10,
    }) for i in (1, 2, 3))
    start, end = datetime(2026, 7, 1), datetime(2026, 7, 4)
    return HistorySelection(families=("NG",), catalog=cards,
        chains=(build_chain(cards, "NG", start, end, 2),))


def loader(**kw):
    frame = pd.DataFrame({"datetime": pd.date_range(kw["start_date"], kw["end_date"],
        freq={"1m": "1min", "1h": "1h"}[kw["timeframe"]], inclusive="left")})
    return frame, kw["instrument_id"]


def check(load=loader, requirements=lambda family: {"1m": 2}):
    return run_chain_preflight(selection(), requirements=requirements,
        start=datetime(2026, 7, 1), end=datetime(2026, 7, 4), load_candles=load)


def test_each_contract_only_checks_its_assigned_days():
    calls = []

    def source(**kw):
        calls.append(kw)
        return loader(**kw)

    result = check(source)
    assert result.ok
    assert result.checked_pairs == 3
    assert all(call["instrument_id"].startswith("uid-") for call in calls)
    assert len(result.detail) == 3


def test_chain_does_not_read_segment_for_gap_statistics_without_coverage():
    calls = []

    def source(**kw):
        calls.append((kw["instrument_id"], kw["start_date"], kw["end_date"]))
        return loader(**kw)

    result = check(source)

    assert result.ok
    segments = [segment for chain in selection().chains for segment in chain]
    assert all(
        sum(uid == segment.card.uid and start == segment.start and end == segment.end
            for uid, start, end in calls) == 1
        for segment in segments
    )
    assert not result.notes


def test_chain_coverage_is_observational_only():
    calls = []

    def coverage(**kw):
        calls.append(kw)
        return {"count": 10}

    result = run_chain_preflight(
        selection(), requirements=lambda _: {"1m": 2}, start=datetime(2026, 7, 1),
        end=datetime(2026, 7, 4), load_candles=loader, coverage=coverage,
    )

    assert result.ok
    assert len(calls) == 3
    assert all("внутренняя целостность не проверена" in note for note in result.notes)


def test_missing_middle_contract_is_not_replaced():
    def source(**kw):
        return (pd.DataFrame(), "uid-2") if kw["instrument_id"] == "uid-2" else loader(**kw)

    result = check(source)
    assert not result.ok
    assert any("NG-2.26" in problem for problem in result.problems)


def test_missing_higher_warmup_is_reported():
    def source(**kw):
        frame, uid = loader(**kw)
        if kw["timeframe"] == "1h":
            frame = frame.loc[frame["datetime"] >= datetime(2026, 7, 1)]
        return frame, uid

    result = check(source, lambda family: {"1m": 2, "1h": 100})
    assert not result.ok
    assert any("1h" in problem and "прогрев" in problem for problem in result.problems)


def test_missing_exit_minutes_rejects_preflight():
    def source(**kw):
        frame, uid = loader(**kw)
        if uid == "uid-1":
            frame = frame.loc[frame["datetime"] < datetime(2026, 7, 2)]
        return frame, uid

    result = check(source)
    assert any("закрытия" in problem for problem in result.problems)


def test_missing_tail_is_not_full_coverage():
    def source(**kw):
        frame, uid = loader(**kw)
        if uid == "uid-3":
            frame = frame.loc[frame["datetime"] < datetime(2026, 7, 3, 12)]
        return frame, uid

    assert any("конец участка" in problem for problem in check(source).problems)
