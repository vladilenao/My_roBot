"""Пары сохранённого состава, которые запуск не запросил, не держат старт.

Живой прогон 19.10: в составе осталась PHOR 15m с обязательством открытой
сделки, хотя 15m-стратегии PHOR заменили на 4h. Потребности прогрева для неё
никто не создавал, и стартовое ожидание висело часами
(retire-unrequired-roster-pairs).
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.assembly import core
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings, SettingError, validate_setting
from src.data.market_store import MarketDataStore
from src.data.poller import LiveMarketDataPoller
from src.data.request_budget import RequestBudget
from src.instruments import Instrument, normalize_instrument
from src.scheduler.timing import MultiTimeframeScheduler

SOURCE = "tbank_exchange"
PHOR_UID = "9978b56f-782a-4a80-a4b1-a48cbecfd194"
NOW = pd.Timestamp("2026-10-19 07:20")
ALL_ORIGINS = ("selected", "obligation", "previous")


def entry(tf, origin="obligation", uid=PHOR_UID, ticker="PHOR"):
    return {"instrument_uid": uid, "interval": tf, "ticker": ticker, "instrument_type": "share",
            "short_name": ticker, "origin": origin, "requirements": {"min_bars": 1}}


def owner(tmp_path, entries):
    """Владелец входа после раннего старта по сохранённому составу."""
    timeline = MultiTimeframeScheduler(["1m", "15m", "1h"], clock=lambda: NOW.to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **_: (pd.DataFrame(), None), timeline, market_store=store,
                            request_budget=RequestBudget(timeline.now, 0))
    store.save_roster(source=SOURCE, entries=entries)
    core._register_saved_roster(cache, store)
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=True))
    return cache, store, worker


def phor(with_uid=True):
    if with_uid:
        return Instrument("PHOR", "PHOR", "share", "PHOR", PHOR_UID)
    return normalize_instrument(("PHOR", "PHOR", "share"))       # live-выбор: UID в карточке нет


def pairs(cache):
    return {(pair["uid"], pair["timeframe"]) for pair in cache.live_pairs()}


def test_unrequested_saved_pair_blocks_startup_until_retired(tmp_path):
    cache, _, worker = owner(tmp_path, [entry("1m"), entry("1h"), entry("15m")])
    for tf in ("1m", "1h"):                         # потребности стратегий и сопровождения
        cache.request_live_history(phor(), tf, 1)
    worker.run_until_idle(NOW)

    assert not worker.startup_complete()            # PHOR 15m никто не прогревает — ожидание вечное
    assert cache.retire_unrequested_live_pairs() == [(PHOR_UID, "15m", "PHOR")]
    assert pairs(cache) == {(PHOR_UID, "1m"), (PHOR_UID, "1h")}
    assert worker.startup_complete()


def test_request_by_ticker_keeps_saved_pair_registered_under_uid(tmp_path):
    cache, *_ = owner(tmp_path, [entry("1m"), entry("1h"), entry("1w"),
                                 entry("1h", origin="selected", uid="u-sber", ticker="SBER")])
    cache.request_live_history(phor(with_uid=False), "1h", 1)
    cache.request_live_history(phor(with_uid=False), "1m", 1)
    cache.request_live_history(Instrument("SBER", "SBER", "share", "SBER", "u-sber"), "1h", 1)
    # Совпадение тикера другого типа инструмента пару не удерживает.
    cache.request_live_history(normalize_instrument(("PHOR", "PHOR", "future")), "1w", 1)

    assert cache.retire_unrequested_live_pairs() == [(PHOR_UID, "1w", "PHOR")]
    assert pairs(cache) == {(PHOR_UID, "1m"), (PHOR_UID, "1h"), ("u-sber", "1h")}
    assert cache.retire_unrequested_live_pairs() == []


def test_obligation_keeps_only_timeframes_its_management_requests(tmp_path):
    cache, store, worker = owner(tmp_path, [entry("1m"), entry("1h"), entry("15m")])
    worker.set_execution_obligations({PHOR_UID: (datetime(2026, 10, 16, 10, 1, tzinfo=timezone.utc), True)})
    for tf in ("1h", "1m"):                         # obligation_requirements: ТФ плана и 1m исполнения
        cache.request_live_history(phor(), tf, 15)

    LiveMarketDataPoller(cache, ["1m"], 1, worker=worker).retire_unrequested_pairs()
    worker.persist_roster()

    roster = {row["interval"]: row["origin"] for row in store.roster(source=SOURCE, origins=ALL_ORIGINS)}
    assert roster == {"1m": "obligation", "1h": "obligation", "15m": "previous"}
    core._register_saved_roster(cache, store)        # следующий запуск её уже не регистрирует
    assert (PHOR_UID, "15m") not in pairs(cache)


def test_retired_pair_returns_when_requested_later(tmp_path):
    cache, store, worker = owner(tmp_path, [entry("1m"), entry("15m")])
    cache.request_live_history(phor(), "1m", 1)
    cache.retire_unrequested_live_pairs()
    assert (PHOR_UID, "15m") not in pairs(cache)

    cache.request_live_history(phor(), "15m", 1)    # 15m снова нужен, например новой стратегии
    worker.run_until_idle(NOW)

    assert (PHOR_UID, "15m") in pairs(cache)
    assert cache.retire_unrequested_live_pairs() == []
    worker.retire_roster_pairs([(PHOR_UID, "15m")])  # снятие до повторного запроса
    worker.persist_roster()
    assert {row["interval"] for row in store.roster(source=SOURCE)} == {"1m", "15m"}


def test_poller_retires_pairs_marks_roster_and_reports_pending(tmp_path):
    cache, _, worker = owner(tmp_path, [entry("1m"), entry("1h"), entry("15m")])
    cache.request_live_history(phor(), "1m", 1)
    cache.request_live_history(phor(), "1h", 1)
    poller = LiveMarketDataPoller(cache, ["1m"], 1, worker=worker)

    assert poller.startup_pending() == ["PHOR 15m: нет прогрева", "PHOR 1h: нет прогрева",
                                        "PHOR 1m: нет прогрева"]
    worker.roster_dirty = False
    assert poller.retire_unrequested_pairs() == [("PHOR", "15m")]
    assert worker.roster_dirty                       # состав пересохранится без снятой пары
    worker.run_until_idle(NOW)
    assert poller.startup_pending() == []
    assert poller.wait_startup(timeout=0)


def test_stream_pending_pairs_are_named_with_status(tmp_path):
    from src.data.stream_ingestion import StreamSupervisor

    cache, _, worker = owner(tmp_path, [entry("1m"), entry("1h")])
    stream = StreamSupervisor(cache, worker, lambda: None, settings=MarketDataSettings(live_transport="stream"))
    stream.sync_roster()
    stream.pairs[(PHOR_UID, "1m")]["status"] = "ready"
    stream.pairs[(PHOR_UID, "1h")]["status"] = "acknowledged"

    assert stream.startup_pending() == {(PHOR_UID, "1h"): "поток: acknowledged"}


def test_startup_waiter_retires_first_then_waits_with_timeout(caplog):
    calls = []
    poller = SimpleNamespace(
        retire_unrequested_pairs=lambda: calls.append("retire") or [("PHOR", "15m")],
        wait_startup=lambda timeout: calls.append(("wait", timeout)) or False,
        startup_pending=lambda: ["PHOR 1h: поток: acknowledged"])
    wait = core._live_startup_waiter(poller, timeout=600)

    with caplog.at_level(logging.INFO):
        assert wait() is False

    assert calls == ["retire", ("wait", 600)]
    assert "PHOR 15m" in caplog.text and "снята с обслуживания" in caplog.text
    assert "600" in caplog.text and "PHOR 1h: поток: acknowledged" in caplog.text


def test_startup_wait_timeout_setting():
    assert MarketDataSettings().startup_wait_timeout_secs == 600
    assert MarketDataSettings(startup_wait_timeout_secs=120).startup_wait_timeout_secs == 120
    with pytest.raises(SettingError):
        validate_setting("startup_wait_timeout_secs", 0)
