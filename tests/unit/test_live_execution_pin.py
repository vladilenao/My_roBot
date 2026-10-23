"""Закреплённый вход исполнения: pin до симуляции, resume без replay, hold."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from src.assembly import contour
from src.data.cache import MarketDataCache
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

NOW = datetime(2026, 10, 12, 12, 2, 30)
SOURCE, UID = "tbank_exchange", "u"


def minutes(*items):
    return pd.DataFrame([dict(datetime=pd.Timestamp(stamp), open=price, high=price + 1, low=price - 1,
                              close=price, volume=1, is_complete=True) for stamp, price in items])


OPEN_POSITION = {UID: (datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc), True)}


def setup(tmp_path, boundaries=None):
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: NOW)
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (pd.DataFrame(), UID), timeline,
                            request_budget=RequestBudget(lambda: NOW, 0), market_store=store)
    cache.contour_id = "emulation"
    instrument = Instrument("SBER", "SBER", "share", "SBER", UID)
    cache.register_live_pair(instrument, UID, "1m")
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 100), ("2026-10-12 12:01", 101)), received_at=NOW)
    broker = MagicMock()
    broker.drain_events.return_value = []
    broker.drain_addressed_events.return_value = []
    storage = MagicMock()
    storage.load_trades.return_value = ()
    # Открытая позиция: 12:00 ещё не применена симуляцией.
    storage.execution_bar_boundaries.side_effect = lambda: dict(OPEN_POSITION if boundaries is None else boundaries)
    with patch("src.assembly.contour.trading_enabled", return_value=True), patch(
        "src.trade_journal.storage.Storage", return_value=storage
    ), patch("src.broker.create_addressable_journal_broker", return_value=broker), patch(
        "src.trade_management.manager.TradeManager", return_value=MagicMock()
    ), patch("src.assembly.contour._load_contracts_metadata", return_value={}), patch("src.assembly.contour.print_contract_metadata"), patch("src.assembly.contour._risk_limits"):
        runtime = contour._build_runtime([instrument], cache, MagicMock(), tmp_path)
    return cache, store, broker, runtime, instrument


def executed(broker):
    return [(call.args[0].replace(tzinfo=None), call.args[1][UID]) for call in broker.track_bar.call_args_list]


def test_simulator_receives_pinned_revision_and_marker_consumes_it(tmp_path):
    cache, store, broker, runtime, _ = setup(tmp_path)
    assert runtime.post_tick({"1m"}) is True
    assert [stamp.minute for stamp, _ in executed(broker)] == [0, 1]
    assert executed(broker)[1][1] == (101.0, 100.0, 102.0, 101.0)
    assert store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")["processed_through"].endswith("12:01:00Z")
    facts = store.live_state(kind="candle", source=SOURCE, instrument_uid=UID, interval="1m",
                             open_time=pd.Timestamp("2026-10-12 12:01"))
    assert facts["input_references"]["emulation"]["state"] == "consumed"


def test_conflicting_minute_holds_execution_without_skipping(tmp_path):
    cache, store, broker, runtime, _ = setup(tmp_path)
    # Отличающаяся версия уже закрытой 12:01 без согласующей выборки — конфликт.
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:01", 105)), received_at=NOW)
    runtime.post_tick({"1m"})
    assert [stamp.minute for stamp, _ in executed(broker)] == [0]
    assert store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")["processed_through"].endswith("12:00:00Z")


def test_restart_after_financial_commit_does_not_replay_minute(tmp_path):
    boundary = {UID: (datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc), False)}
    cache, store, broker, runtime, _ = setup(tmp_path, boundary)
    assert store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")["processed_through"] is None
    runtime.post_tick({"1m"})
    # 12:00 уже закоммичена финансово: marker догнал её, симулирована только 12:01.
    assert [stamp.minute for stamp, _ in executed(broker)] == [1]
    assert store.cursors(source=SOURCE, instrument_uid=UID, interval="1m")["processed_through"].endswith("12:01:00Z")


def test_correction_during_started_package_becomes_integrity_hold(tmp_path):
    cache, store, broker, runtime, instrument = setup(tmp_path)
    reference, pinned = cache.pin_execution_input(instrument, pd.Timestamp("2026-10-12 12:00"))
    cache.accept_rpc_frame(UID, "1m", minutes(("2026-10-12 12:00", 99)), received_at=NOW)
    assert pinned["close"] == 100 and store.pinned_frame(reference)["close"] == 100
    runtime.post_tick({"1m"})
    # Начатая минута завершается на закреплённой версии, затем hold UID.
    assert executed(broker)[0][1][3] == 100.0
    assert cache.pair_status(instrument, "1m")["state"] == "INTEGRITY_HOLD"
    assert [stamp.minute for stamp, _ in executed(broker)] == [0]
    runtime.post_tick({"1m"})
    assert [stamp.minute for stamp, _ in executed(broker)] == [0]
