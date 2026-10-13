from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.history.preflight import Pair, run_chain_preflight
from src.history.selection import HistorySelection
from src.instruments.futures import FuturesCard

START = datetime(2025, 1, 8)
END = datetime(2025, 3, 15, 12)
PROVIDER = SimpleNamespace(base_url="sessions", snapshot_generation=42)
TFS = {"1m": "1min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1d"}


def selected():
    cards = tuple(FuturesCard.from_row({"instrument_id": f"uid-{month}", "ticker": f"NG-{month}.25",
        "name": f"NG-{month}.25 Газ", "first_trade_date": "2024-12-01T00:00:00Z",
        "expiration_date": expiration, "min_price_increment": 1, "min_price_increment_amount": 1,
        "initial_margin_on_buy": 100, "initial_margin_on_sell": 100}) for month, expiration in (
        (1, "2025-01-29T00:00:00Z"), (2, "2025-02-26T00:00:00Z"), (3, "2025-03-27T00:00:00Z")))
    return HistorySelection(families=("NG",), catalog=cards).planned(START, END, {}, 2)


class Sessions:
    def __init__(self, *, truncated=False, no_exit=False):
        self.calls = []
        self.truncated, self.no_exit = truncated, no_exit
        self.series = {}
        for tf, freq in TFS.items():
            dates = []
            for day in pd.bdate_range("2024-12-02", "2025-03-17"):
                dates.extend([day] if tf == "1d" else pd.date_range(day+pd.Timedelta(hours=7),
                    day+pd.Timedelta(hours=20, minutes=49), freq=freq))
            self.series[tf] = pd.Series(dates)

    def __call__(self, **kw):
        self.calls.append(kw)
        uid = kw["instrument_id"] or "fixed"
        stamps = self.series[kw["timeframe"]]
        if self.truncated and uid == "uid-1":
            stamps = stamps[stamps < datetime(2025, 1, 27)]
        if self.no_exit and uid == "uid-1" and kw["timeframe"] == "1m":
            stamps = stamps[(stamps < datetime(2025, 1, 27)) | (stamps >= datetime(2025, 1, 29))]
        stamps = stamps[(stamps >= kw["start_date"]) & (stamps <= kw["end_date"])]
        return pd.DataFrame({"datetime": stamps, "close": 100}), uid


def preflight(source, **kw):
    return run_chain_preflight(selected(), requirements=lambda _: {tf: 2 for tf in TFS},
        start=START, end=END, load_candles=source, client_provider=PROVIDER, **kw)


def test_ng_cutoffs_and_saturday_end_do_not_require_sunday_bars():
    source = Sessions()
    report = preflight(source)
    assert report.ok
    assert len(report.boundaries) == 15
    minutes = [item for item in report.boundaries if item.pair.timeframe == "1m"]
    assert [item.boundary for item in minutes] == [datetime(2025, 1, 27), datetime(2025, 2, 24), END]
    assert [item.last_before for item in minutes] == [datetime(2025, 1, 24, 20, 49),
        datetime(2025, 2, 21, 20, 49), datetime(2025, 3, 14, 20, 49)]
    assert [item.first_after for item in minutes] == [datetime(2025, 1, 27, 7),
        datetime(2025, 2, 24, 7), datetime(2025, 3, 17, 7)]
    assert all(item.snapshot_generation == 42 for item in report.boundaries)
    assert all(item.ok for item in report.boundaries)
    assert "перерыв на границе подтверждён" in report.message()


@pytest.mark.parametrize("source", [Sessions(truncated=True), Sessions(no_exit=True)])
def test_old_contract_tail_and_minute_exit_cannot_use_new_contract(source):
    report = preflight(source)
    assert not report.ok
    assert any("NG-1.25" in text for text in report.problems)
    assert any("закрытия" in text for text in report.problems)


def test_start_anchored_by_driver_not_daily_bar():
    source = Sessions()
    # Дневной бар открылся в 00:00 до начала, но минутный поток покрывает 12:00.
    start = datetime(2025, 1, 8, 12)
    choice = selected().planned(start, END, {}, 2)
    result = run_chain_preflight(choice, requirements=lambda _: {tf: 2 for tf in TFS},
        start=start, end=END, load_candles=source, client_provider=PROVIDER)
    assert result.ok
    assert result.start == start


def test_shift_is_stable_on_rerun():
    source = Sessions()
    first = preflight(source)
    assert first.start == datetime(2025, 1, 8, 7)
    rerun = run_chain_preflight(selected().planned(first.start, END, {}, 2),
        requirements=lambda _: {tf: 2 for tf in TFS}, start=first.start, end=END,
        load_candles=source, client_provider=PROVIDER)
    assert rerun.ok
    assert rerun.start == first.start


def test_mixed_preserves_fixed_and_chain_boundary_evidence():
    source = Sessions()
    # Fixed начало на существующей минуте: сдвиг начала не относится к этому change.
    start = datetime(2025, 1, 8, 7, 2)
    choice = selected().planned(start, END, {}, 2)
    result = run_chain_preflight(choice, requirements=lambda _: {"1m": 2}, start=start, end=END,
        load_candles=source, client_provider=PROVIDER, fixed_warmup=2,
        fixed_pairs=[Pair("SBER", "share", "1m", "fixed", "SBER")])
    assert result.ok
    assert {item.pair.instrument_id for item in result.boundaries} == {"fixed", "uid-1", "uid-2", "uid-3"}
    assert all(item.source_id == result.boundaries[0].source_id for item in result.boundaries)
