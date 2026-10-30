"""Atomic market-only версия, конфликт и неизменяемый выбранный вход."""

from dataclasses import replace
from datetime import datetime

import pytest

from src.data.live_state import normalize_rpc_candle
from src.data.market_store import MarketDataStore, SCHEMA_VERSION


IDENTITY = dict(source="tbank_exchange", instrument_uid="u", interval="1m")
OPEN = datetime(2026, 10, 12, 12)


def observation(price=100, *, closed=True, transport="rpc", source=1):
    row = dict(datetime=OPEN, open=price, high=price+1, low=price-1, close=price, volume=1, is_complete=closed)
    result = normalize_rpc_candle(row, uid="u", timeframe="1m", received_at="2026-10-12T12:01:02Z", actual_source=source)
    return replace(result, transport=transport)


def state(store):
    return store.live_state(kind="candle", open_time=OPEN, **IDENTITY)


def frame(store):
    return store.current_frame(**IDENTITY)


def test_identical_cross_transport_repeat_has_one_revision_and_export_change(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    assert store.accept_observation(observation())["changed"]
    result = store.accept_observation(observation(transport="stream"))
    assert result == dict(state="accepted", revision=1, changed=False)
    assert len(store.changes_after(store.producer_id, 0)["changes"]) == 1
    assert state(store)["last_observation"]["transport"] == "stream"
    assert state(store)["last_observation"]["actual_source"] == 1


def test_preliminary_never_downgrades_published_closed_values(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation())
    result = store.accept_observation(observation(200, closed=False))
    assert result["state"] == "preliminary"
    assert frame(store)["close"].iloc[0] == 100
    assert frame(store)["is_complete"].iloc[0]
    # Legacy writer также не должен уронить уже закрытый вход.
    store.record_candles(candles=[observation(200, closed=False).as_row()], **IDENTITY)
    assert frame(store)["close"].iloc[0] == 100


def test_old_warmup_snapshot_is_a_conflict_until_fenced_recheck(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(101, transport="stream"))
    result = store.accept_observation(observation(100))
    assert result["state"] == "conflict"
    assert frame(store)["close"].iloc[0] == 101
    assert state(store)["conflict"]["candidate_revision"] == 2
    fence = state(store)["observed_sequence"]
    resolved = store.accept_observation(observation(100), reconcile_sequence=fence)
    assert resolved["state"] == "accepted" and resolved["revision"] == 2
    assert frame(store)["close"].iloc[0] == 100
    assert "conflict" not in state(store)


def test_correction_arriving_during_recheck_invalidates_fence(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.accept_observation(observation(101, transport="stream"))
    fence = state(store)["observed_sequence"]
    store.accept_observation(observation(102, transport="stream"))
    assert store.accept_observation(observation(101), reconcile_sequence=fence)["state"] == "conflict"
    assert frame(store)["close"].iloc[0] == 100


def test_oscillation_back_to_old_values_does_not_reuse_a_stale_fence(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.accept_observation(observation(101, transport="stream"))
    store.accept_observation(observation(100), reconcile_sequence=state(store)["observed_sequence"])
    fence = state(store)["observed_sequence"]
    store.accept_observation(observation(101, transport="stream"))
    assert store.accept_observation(observation(100), reconcile_sequence=fence)["state"] == "conflict"


def test_pinned_input_and_incident_survive_restart_and_reconciliation(tmp_path):
    path = tmp_path / "market.sqlite3"
    store = MarketDataStore(path)
    store.accept_observation(observation(100))
    reference = store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")
    reference["state"] = "consumed"
    store.accept_observation(observation(101, transport="stream"))
    reopened = MarketDataStore(path)
    assert reopened.pinned_frame(reference)["close"] == 100
    held = reopened.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u")
    assert not held.get("integrity_hold") and held["incident"]["consumed_revision"] == reference["revision"]
    # Повтор начатого пакета получает исходную revision.
    assert reopened.pin_input(open_time=OPEN, **IDENTITY, contour="emulation") == reference
    reopened.accept_observation(observation(101), reconcile_sequence=state(reopened)["observed_sequence"])
    assert reopened.pinned_frame(reference)["close"] == 100
    assert not reopened.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u").get("integrity_hold")


def test_market_operational_state_preserves_producer_and_export_schema(tmp_path):
    path = tmp_path / "market.sqlite3"
    store = MarketDataStore(path)
    producer = store.producer_id
    store.accept_observation(observation())
    store.save_live_state(dict(epoch=3, lower="2026-10-12T09:00:00Z", upper="2026-10-12T12:00:00Z"),
                          kind="pair", **IDENTITY)
    reopened = MarketDataStore(path)
    assert reopened.producer_id == producer
    assert reopened.source_info().schema_version == SCHEMA_VERSION
    assert reopened.live_state(kind="pair", **IDENTITY)["epoch"] == 3
    export = reopened.changes_after(producer, 0)["changes"][0]["candle"]
    assert "epoch" not in export and "input_reference" not in export and "input_references" not in export


def test_inflight_pin_is_not_itself_financial_execution(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    store.accept_observation(observation(101, transport="stream"))
    held = store.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u")
    assert not held.get("integrity_hold") and not held.get("pending_input_conflicts")
    assert held["incident"]["reason"] == "consumed_input_changed"
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")
    held = store.live_state(kind="uid", source=IDENTITY["source"], instrument_uid="u")
    assert not held.get("integrity_hold")


def test_receipt_resolved_and_processed_are_independent(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation(100))
    assert store.cursors(**IDENTITY)["received_through"] is not None
    assert store.cursors(**IDENTITY)["resolved_through"] is None
    assert store.cursors(**IDENTITY)["processed_through"] is None
    store.mark_resolved(open_time=OPEN, **IDENTITY)
    assert store.cursors(**IDENTITY)["processed_through"] is None
    store.mark_processed(open_time=OPEN, **IDENTITY, contour="emulation")
    assert store.cursors(**IDENTITY)["processed_through"] == store.cursors(**IDENTITY)["resolved_through"]


def test_contiguous_resolution_uses_meaningful_lower_not_received_high_water(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    second = OPEN.replace(minute=1)
    third = OPEN.replace(minute=2)
    store.accept_observation(observation(100))
    late = replace(observation(102), open_time=third)
    store.accept_observation(late)
    assert store.resolve_contiguous_prefix(meaningful_lower=OPEN, horizon=third, **IDENTITY) == OPEN
    assert store.cursors(**IDENTITY)["received_through"].endswith("12:02:00Z")
    assert store.cursors(**IDENTITY)["resolved_through"].endswith("12:00:00Z")
    store.accept_observation(replace(observation(101), open_time=second))
    assert store.resolve_contiguous_prefix(meaningful_lower=OPEN, horizon=third, **IDENTITY) == third


def test_analysis_cursor_does_not_advance_external_processed_frontier(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.accept_observation(observation())
    store.mark_analyzed(assignment_id="a", open_time=OPEN, **IDENTITY, contour="emulation")
    assert store.analysis_cursor(assignment_id="a", **IDENTITY, contour="emulation") == OPEN
    assert store.cursors(**IDENTITY)["processed_through"] is None


def test_calendar_resolution_recomputes_persisted_meaningful_prefix(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    second = OPEN.replace(minute=1)
    third = OPEN.replace(minute=2)
    store.accept_observation(observation(100))
    store.accept_observation(replace(observation(102), open_time=third))
    store.save_live_state({"coverage_lower": OPEN.isoformat()}, kind="pair", **IDENTITY)
    store.set_interval_state(open_time=second, state="scheduled_closed", evidence="calendar", **IDENTITY)
    assert store.cursors(**IDENTITY)["resolved_through"].endswith("12:02:00Z")


def test_unknown_missing_or_conflicting_input_cannot_be_pinned(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    with pytest.raises(ValueError, match="завершённой"):
        store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
    store.accept_observation(observation())
    store.accept_observation(observation(101, transport="stream"))
    with pytest.raises(ValueError, match="неподтверждённый"):
        store.pin_input(open_time=OPEN, **IDENTITY, contour="emulation")
