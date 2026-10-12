"""Совместимый source-profile и UTC-границы без запросов брокеру."""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from t_tech.invest.schemas import HistoricCandle, Quotation

from src.api.client import LIVE_CANDLE_SOURCE
from src.api.market_data_stream import candle_subscription_request
from src.data.loader import load_candles
from src.data.timeutil import to_utc_naive
from src.scheduler.timing import CandleScheduler


@pytest.mark.parametrize("with_budget", [False, True])
def test_unary_and_stream_request_same_unspecified_source_profile(with_budget):
    sent = []
    stamp = datetime.fromisoformat("2026-10-12T15:00:00+03:00")
    quote = Quotation(units=100, nano=0)
    candle = HistoricCandle(time=stamp, open=quote, high=quote, low=quote, close=quote,
                           volume=1, is_complete=True)
    def get_candles(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(candles=[candle])
    def get_all_candles(**kwargs):
        sent.append(kwargs)
        return iter([candle])
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(get_all_candles=get_all_candles,
                                  market_data=SimpleNamespace(get_candles=get_candles))
    class Budget:
        def call(self, method, **kwargs):
            for name in ("key", "wait", "pace"):
                kwargs.pop(name, None)
            return method(**kwargs)
    frame, uid = load_candles("SBER", "share", "1m", instrument_id="u", client_provider=Provider(),
        request_budget=Budget() if with_budget else None, single_attempt=True,
        start_date=datetime.fromisoformat("2026-10-12T14:59:00+03:00"),
        end_date=datetime.fromisoformat("2026-10-12T15:02:00+03:00"))
    assert uid == "u"
    assert frame["datetime"].iloc[0] == pd.Timestamp("2026-10-12T12:00:00")
    assert set(frame.columns) == {"datetime", "open", "high", "low", "close", "volume", "is_complete"}
    stream = candle_subscription_request([("u", "1m")])
    assert all(request["candle_source_type"] is LIVE_CANDLE_SOURCE for request in sent)
    assert stream.subscribe_candles_request.candle_source_type == LIVE_CANDLE_SOURCE.value == 0


def test_loader_does_not_invent_complete_for_a_candle_missing_the_flag():
    quote = SimpleNamespace(units=100, nano=0)
    candle = SimpleNamespace(time=datetime.fromisoformat("2026-10-12T12:00:00+00:00"),
        open=quote, high=quote, low=quote, close=quote, volume=1)
    class Provider:
        @contextmanager
        def client_context(self, token):
            yield SimpleNamespace(get_all_candles=lambda **kwargs: iter([candle]))
    frame, _ = load_candles("SBER", "share", "1m", instrument_id="u", client_provider=Provider(),
        start_date="2026-10-12", end_date="2026-10-13", single_attempt=True)
    assert not frame["is_complete"].iloc[0]


@pytest.mark.parametrize("tf,opening,closing", [
    ("1m", "2026-10-12T12:00:00Z", "2026-10-12T12:01:00Z"),
    ("15m", "2026-10-12T12:00:00Z", "2026-10-12T12:15:00Z"),
    ("1h", "2026-10-12T12:00:00Z", "2026-10-12T13:00:00Z"),
    ("4h", "2026-10-12T12:00:00Z", "2026-10-12T16:00:00Z"),
    ("1d", "2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z"),
    ("1w", "2026-10-12T00:00:00Z", "2026-10-19T00:00:00Z"),
    ("1M", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
])
def test_existing_native_utc_grid_is_preserved_instead_of_30_day_month(tf, opening, closing):
    grid = CandleScheduler(tf)
    assert grid.bar_close(to_utc_naive(opening)) == to_utc_naive(closing)
    assert to_utc_naive("2026-10-12T15:00:00+03:00") == to_utc_naive("2026-10-12T12:00:00Z")
