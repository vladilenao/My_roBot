"""Раздельные закрытие, receipt, source и неизменяемость входных фактов."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from src.api.market_data_stream import StreamCandle
from src.data.live_state import StreamClosureEvidence, normalize_rpc_candle, normalize_stream_candle


def packet():
    return StreamCandle("u", "1m", datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
        datetime(2026, 10, 12, 12, 0, 20, tzinfo=timezone.utc),
        (Decimal(100), Decimal(101), Decimal(99), Decimal(100)), 1, 1)


def ack():
    return StreamClosureEvidence("u", "1m", 2, 1, 1, True)


def test_ack_closed_grid_is_not_last_trade_timestamp_or_finality():
    observation = normalize_stream_candle(packet(), evidence=ack(), epoch=2, received_at="2026-10-12T12:01:02Z")
    assert observation.closed is True
    assert observation.close_time == datetime(2026, 10, 12, 12, 1)
    assert observation.last_trade_time == datetime(2026, 10, 12, 12, 0, 20)
    assert observation.received_at == datetime(2026, 10, 12, 12, 1, 2)
    assert observation.closure_evidence == "ack_waiting_close_grid"
    assert observation.actual_source == 1 and observation.requested_source == 0
    assert not hasattr(observation, "final")
    with pytest.raises(FrozenInstanceError):
        observation.closed = False


@pytest.mark.parametrize("evidence", [None, replace(ack(), uid="other"), replace(ack(), timeframe="15m"),
    replace(ack(), epoch=1), replace(ack(), status=5), replace(ack(), action=2),
    replace(ack(), waiting_close=False), replace(ack(), requested_source=1), replace(ack(), echoed_source=1)])
def test_unconfirmed_or_stale_ack_never_defaults_to_closed(evidence):
    observation = normalize_stream_candle(packet(), evidence=evidence, epoch=2, received_at="2026-10-12T12:01:02Z")
    assert observation.closed is False


def test_early_packet_even_in_waiting_close_mode_is_not_closed():
    observation = normalize_stream_candle(packet(), evidence=ack(), epoch=2, received_at="2026-10-12T12:00:59Z")
    assert observation.closed is False


@pytest.mark.parametrize("flag", [None, False, 1, "true", object()])
def test_rpc_missing_false_or_invalid_complete_flag_is_not_true(flag):
    row = dict(datetime="2026-10-12T15:00:00+03:00", open=100, high=101, low=99, close=100, volume=1)
    if flag is not None:
        row["is_complete"] = flag
    assert not normalize_rpc_candle(row, uid="u", timeframe="1m", received_at="2026-10-12T12:01:02Z").closed


@pytest.mark.parametrize("flag", [True, np.bool_(True)])
def test_rpc_complete_normalizes_utc_before_dropping_zone(flag):
    row = dict(datetime="2026-10-12T15:00:00+03:00", open=100, high=101, low=99, close=100, volume=1,
               is_complete=flag)
    observation = normalize_rpc_candle(row, uid="u", timeframe="1m", received_at="2026-10-12T12:01:02Z", actual_source=1)
    assert observation.open_time == datetime(2026, 10, 12, 12)
    assert observation.closed and observation.closure_evidence == "rpc_is_complete"
    assert set(observation.as_row()) == {"datetime", "open", "high", "low", "close", "volume", "is_complete"}


def test_unknown_actual_source_is_not_silently_unspecified():
    with pytest.raises(ValueError, match="источник"):
        normalize_stream_candle(replace(packet(), source=99), evidence=ack(), epoch=2, received_at="2026-10-12T12:01:02Z")
