"""Сбор данных до выбора инструментов: порядок старта и сохранённый состав."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import run
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.lanes import CompletenessLane, EvidenceLane
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument, normalize_instrument
from src.instruments.model import instrument_key
from src.scheduler.timing import MultiTimeframeScheduler

SOURCE = "tbank_exchange"


def test_live_launch_starts_market_data_before_instrument_selection(monkeypatch, tmp_path):
    order = []
    budget = object()
    monkeypatch.setattr(run, "MARKET_DATA_SETTINGS", MarketDataSettings(independent_lanes=True))
    monkeypatch.setattr(run, "_start_early_market_data",
                        lambda state_dir, provider: order.append("early") or SimpleNamespace(request_budget=budget))

    def select(**kwargs):
        order.append("select")
        assert kwargs["request_budget"] is budget          # выбор идёт через тот же арбитр
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "select_instruments", select)
    with pytest.raises(KeyboardInterrupt):
        run._launch(state_dir=tmp_path, client_provider=SimpleNamespace(), clock=None,
                    session=SimpleNamespace(), channel_names=[])
    assert order == ["early", "select"]


def test_history_launch_does_not_start_lanes(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "MARKET_DATA_SETTINGS", MarketDataSettings(independent_lanes=True))
    monkeypatch.setattr(run, "_start_early_market_data", lambda *a: pytest.fail("ранний старт в истории"))

    def select(**kwargs):
        assert kwargs["request_budget"] is None
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "select_instruments", select)
    with pytest.raises(KeyboardInterrupt):
        run._launch(state_dir=tmp_path, client_provider=SimpleNamespace(), clock=SimpleNamespace(now=lambda: None),
                    session=SimpleNamespace(), channel_names=[], history=True)


def owner(tmp_path):
    timeline = MultiTimeframeScheduler(["1m", "1h"], clock=lambda: pd.Timestamp("2026-10-13 10:00").to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **_: (pd.DataFrame(), None), timeline, market_store=store,
                            request_budget=RequestBudget(timeline.now, 0))
    return cache, store


def test_saved_roster_is_registered_and_unselected_pairs_are_retired(tmp_path):
    cache, store = owner(tmp_path)
    def entry(uid, ticker, tf, origin):
        return {"instrument_uid": uid, "interval": tf, "ticker": ticker,
                                                 "instrument_type": "share", "short_name": ticker, "origin": origin,
                                                 "requirements": {"min_bars": 60}}
    store.save_roster(source=SOURCE, entries=[entry("u-sber", "SBER", "1h", "selected"),
                                              entry("u-phor", "PHOR", "1h", "selected"),
                                              entry("u-ng", "NGZ6", "1m", "obligation")])
    assert run._register_saved_roster(cache, store) == 3
    assert {(p["uid"], p["timeframe"], p["min_bars"]) for p in cache.live_pairs()} == {
        ("u-sber", "1h", 60), ("u-phor", "1h", 60), ("u-ng", "1m", 60)}

    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=True))
    worker.set_execution_obligations({"u-ng": (pd.Timestamp("2026-10-13 09:00"), True)})
    run._retire_unselected_pairs(cache, [Instrument("SBER", "SBER", "share", "SBER", "u-sber")], worker)
    assert {p["uid"] for p in cache.live_pairs()} == {"u-sber", "u-ng"}   # PHOR не выбран — снят
    worker.persist_roster()
    assert {row["instrument_uid"]: row["origin"] for row in store.roster(source=SOURCE)} == {
        "u-sber": "selected", "u-ng": "obligation"}


@pytest.mark.parametrize("interval,expected", [(0, 62), (5, 1)])
def test_stream_handoff_is_not_paced_by_global_window_with_lanes(tmp_path, interval, expected):
    now = pd.Timestamp("2026-10-13 10:00:30")
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: now.to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")

    def loader(**kwargs):
        frame = pd.DataFrame({"datetime": [pd.Timestamp("2026-10-13 09:59")], "open": [1.0], "high": [1.0],
                              "low": [1.0], "close": [1.0], "volume": [1], "is_complete": [True]})
        # Как настоящий загрузчик: запрос идёт через арбитр с его окном темпа.
        return kwargs["request_budget"].call(lambda: frame, pace=kwargs["pace"],
                                             wait=kwargs["wait_for_budget"]), kwargs["instrument_id"]

    cache = MarketDataCache(loader, timeline, market_store=store, request_budget=RequestBudget(timeline.now, interval))
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=True))
    ready = []
    for index in range(62):
        item = Instrument(f"T{index}", f"T{index}", "share", f"T{index}", f"u{index}")
        cache.register_live_pair(item, f"u{index}", "1m")
        cache.mark_live_history(f"u{index}", "1m", done=True)
        worker.request_handoff(f"u{index}", "1m", 1, acknowledged_at=now,
                               on_ready=lambda uid, tf, epoch: ready.append(uid))
    for _ in range(100):
        if not worker.step(now):
            break
    assert len(ready) == expected   # с окном 5 с — одна пара за момент, без него — все


PHOR_UID = "9978b56f-782a-4a80-a4b1-a48cbecfd194"   # UID с цифры сортируется раньше тикера


def started_owner(tmp_path, clock):
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(lambda **_: (pd.DataFrame(), None), timeline, market_store=store,
                            request_budget=RequestBudget(lambda: clock[0], 0))
    def entry(uid, ticker):
        return {"instrument_uid": uid, "interval": "1m", "ticker": ticker,
                                     "instrument_type": "share", "short_name": ticker, "origin": "selected",
                                     "requirements": {"min_bars": 1}}
    store.save_roster(source=SOURCE, entries=[entry(PHOR_UID, "PHOR"), entry("0-sber", "SBER")])
    run._register_saved_roster(cache, store)                  # полосы стартуют до выбора
    for uid in (PHOR_UID, "0-sber"):
        cache.mark_live_history(uid, "1m", done=True)
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=True))
    completeness = CompletenessLane(worker)
    worker.evidence = EvidenceLane(worker, completeness)
    worker.completeness = completeness
    return cache, store, worker, completeness


def journal_obligations(cache, selected):
    """Обязательства так, как их собирает запуск: журнал хранит вход PHOR под тикером."""
    journal = {"PHOR": (datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc), True)}
    aliases = {alias: instrument_key(item) for item in selected for alias in (instrument_key(item), item.ticker)}
    return run._market_obligations(journal, aliases, selected, cache)


def test_obligation_under_ticker_reaches_pair_registered_under_uid(tmp_path):
    clock = [pd.Timestamp("2026-10-14 04:22:30")]
    cache, store, worker, completeness = started_owner(tmp_path, clock)
    phor = normalize_instrument(("PHOR", "PHOR", "share"))     # live-выбор: без UID в карточке

    worker.set_execution_obligations(journal_obligations(cache, [phor]))
    clock[0] = pd.Timestamp("2026-10-14 12:00")
    pair = cache.live_pair(PHOR_UID, "1m")

    assert completeness.window_lower(pair, clock[0]) <= pd.Timestamp("2026-10-13 12:00")
    assert completeness._level(pair) == 0                       # первая очередь полноты
    run._retire_unselected_pairs(cache, [normalize_instrument(("SBER", "SBER", "share"))], worker)
    assert cache.live_pair(PHOR_UID, "1m") is not None           # с обязательством не снимается
    worker.persist_roster()
    assert {row["instrument_uid"]: row["origin"] for row in store.roster(source=SOURCE)}[PHOR_UID] == "obligation"


def test_pair_uid_finds_saved_roster_pair_for_instrument_without_card_uid(tmp_path):
    clock = [pd.Timestamp("2026-10-14 04:22:30")]
    cache, *_ = started_owner(tmp_path, clock)
    phor = normalize_instrument(("PHOR", "PHOR", "share"))

    assert cache.instrument_uid(phor, "1m") is None             # по адресу инструмента пары нет
    assert cache.pair_uid(phor, "1m") == PHOR_UID
    assert cache.pair_uid(phor, "15m") is None                   # пары этого ТФ нет
    assert cache.pair_uid(normalize_instrument(("PHOR", "PHOR", "future")), "1m") is None


def test_closed_trade_narrows_window_back_to_launch(tmp_path):
    clock = [pd.Timestamp("2026-10-14 04:22:30")]
    cache, _, worker, completeness = started_owner(tmp_path, clock)
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    worker.set_execution_obligations(journal_obligations(cache, [phor]))
    clock[0] = pd.Timestamp("2026-10-14 12:00")
    pair = cache.live_pair(PHOR_UID, "1m")
    assert completeness._level(pair) == 0

    worker.set_execution_obligations({})                         # вход снят или исполнен
    assert completeness.window_lower(pair, clock[0]) > pd.Timestamp("2026-10-14 04:00")
    assert completeness._level(pair) == 3


@pytest.mark.parametrize("selected_first", [False, True])
def test_execution_lower_applies_whatever_registration_order(tmp_path, selected_first):
    clock = [pd.Timestamp("2026-10-14 04:22:30")]
    cache, *_ = started_owner(tmp_path, clock)
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    boundary = pd.Timestamp("2026-10-13 12:00")
    if selected_first:
        cache.register_live_pair(phor, PHOR_UID, "1m")
        cache.note_execution_lower(phor, boundary)
    else:
        cache.note_execution_lower(phor, boundary)             # пара выбора ещё не зарегистрирована
        cache.register_live_pair(phor, PHOR_UID, "1m")
    assert cache._live_members[(PHOR_UID, "1m")]["execution_lower"] == boundary
    assert cache.live_floor(PHOR_UID, "1m") is None


def test_market_obligations_map_journal_ids_to_pair_uids(tmp_path):
    clock = [pd.Timestamp("2026-10-14 04:22:30")]
    cache, *_ = started_owner(tmp_path, clock)
    def at(hour):
        return (datetime(2026, 10, 13, hour, tzinfo=timezone.utc), True)
    journal = {"PHOR": at(12), PHOR_UID: at(11),               # одна пара под двумя адресами
               "SBER": at(13),                                  # не выбран, пара сохранённого состава есть
               "EDZ6": at(14)}                                  # пары ещё нет — переведёт следующий тик
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    aliases = {"PHOR": "PHOR"}

    result = run._market_obligations(journal, aliases, [phor], cache)

    assert result == {PHOR_UID: at(11), "0-sber": at(13), "EDZ6": at(14)}
