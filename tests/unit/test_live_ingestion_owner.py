"""Оба транспорта публикуют один вход; trade-read не инициирует сеть."""

from datetime import datetime
from decimal import Decimal
import threading

import pandas as pd
import pytest

from src.api.market_data_stream import StreamCandle
from src.data.cache import MarketDataCache
from src.data.live_state import StreamClosureEvidence
from src.data.market_store import MarketDataStore
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler


def setup_owner(tmp_path):
    now = datetime(2026, 10, 12, 12, 1, 2)
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: now)
    def forbidden(**kwargs):
        pytest.fail("Сеть из чтения торгового входа")
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(forbidden, timeline, market_store=store)
    cache.contour_id = "emulation"
    instrument = Instrument("SBER", "SBER", "share", "SBER", "u")
    cache.register_live_pair(instrument, "u", "1m")
    cache._live_evidence["u", "1m"] = StreamClosureEvidence("u", "1m", 1, 1, 1, True)
    return cache, store, instrument, now


def candle(price=100):
    return StreamCandle("u", "1m", datetime(2026, 10, 12, 12), datetime(2026, 10, 12, 12, 0, 20),
                        tuple(Decimal(value) for value in (price, price+1, price-1, price)), 1, 1)


def rpc_frame(price=100):
    frame = pd.DataFrame([dict(datetime=datetime(2026, 10, 12, 12), open=price, high=price+1,
                              low=price-1, close=price, volume=1, is_complete=True)])
    frame.attrs["actual_candle_sources"] = {"2026-10-12T12:00:00": 1}
    return frame


def test_stream_and_rpc_have_one_canonical_path_and_one_change(tmp_path):
    cache, store, instrument, now = setup_owner(tmp_path)
    cache.accept_stream_candle(candle(), epoch=1, received_at=now)
    cache.accept_rpc_frame("u", "1m", rpc_frame(), received_at=now)
    assert len(cache.frame_for(instrument, "1m")) == 1
    assert len(store.changes_after(store.producer_id, 0)["changes"]) == 1
    frame = cache.frame_for(instrument, "1m")
    frame.loc[0, "close"] = 999
    assert cache.frame_for(instrument, "1m")["close"].iloc[0] == 100


def test_warmup_overlap_does_not_overwrite_stream_before_recheck(tmp_path):
    cache, store, instrument, now = setup_owner(tmp_path)
    cache.accept_stream_candle(candle(101), epoch=1, received_at=now)
    cache.accept_rpc_frame("u", "1m", rpc_frame(100), received_at=now)
    assert cache.frame_for(instrument, "1m")["close"].iloc[0] == 101
    fences = cache.reconciliation_fences("u", "1m", rpc_frame())
    cache.accept_rpc_frame("u", "1m", rpc_frame(100), received_at=now, fences=fences)
    assert cache.frame_for(instrument, "1m")["close"].iloc[0] == 100


def test_stale_epoch_does_not_publish_or_downgrade_closed_input(tmp_path):
    cache, store, instrument, now = setup_owner(tmp_path)
    cache.accept_stream_candle(candle(101), epoch=1, received_at=now)
    cache._live_evidence["u", "1m"] = StreamClosureEvidence("u", "1m", 2, 1, 1, True)
    assert cache.accept_stream_candle(candle(100), epoch=1, received_at=now)["state"] == "preliminary"
    assert cache.frame_for(instrument, "1m")["close"].iloc[0] == 101


def test_registered_financial_alias_cannot_be_rebound_to_another_uid(tmp_path):
    cache, store, _, _ = setup_owner(tmp_path)
    legacy = Instrument("SBER", "SBER", "share", "SBER")
    cache.register_live_pair(legacy, "u", "1m")
    with pytest.raises(ValueError, match="Неоднозначный"):
        cache.register_live_pair(legacy, "another-uid", "1m")


def test_network_worker_and_stream_reader_serialize_acceptance(tmp_path):
    cache, store, instrument, now = setup_owner(tmp_path)
    gate = threading.Barrier(3)
    errors = []
    def receive(call):
        try:
            gate.wait()
            call()
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=receive, args=(lambda: cache.accept_stream_candle(candle(), epoch=1, received_at=now),)),
               threading.Thread(target=receive, args=(lambda: cache.accept_rpc_frame("u", "1m", rpc_frame(), received_at=now),))]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(2)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(store.changes_after(store.producer_id, 0)["changes"]) == 1


def test_execution_pin_is_immutable_while_correction_arrives(tmp_path):
    cache, store, instrument, now = setup_owner(tmp_path)
    cache.accept_stream_candle(candle(), epoch=1, received_at=now)
    reference, row = cache.pin_execution_input(instrument, datetime(2026, 10, 12, 12))
    cache.accept_stream_candle(candle(101), epoch=1, received_at=now)
    assert row["close"] == 100
    assert store.pinned_frame(reference)["close"] == 100


def test_trade_read_and_missing_dependency_only_enqueue_background_need(tmp_path):
    cache, _, instrument, _ = setup_owner(tmp_path)
    assert cache.prepare_live_history(instrument, "15m", 100).empty
    cache.ensure_loaded(instrument, "1h")
    cache.refresh_live()
    cache.refresh_if_new_candle("1h", force=True)
    assert cache._pending_live_history[cache._key(instrument, "15m")][1] == 100
    assert cache._pending_live_history[cache._key(instrument, "1h")][1] == 1
