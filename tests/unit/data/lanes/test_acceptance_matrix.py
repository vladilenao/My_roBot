"""Acceptance Matrix design add-independent-market-data-lanes (задача 9.1).

Сценарии — на управляемых часах и фиктивной бирже. Часть сценариев проверена
тестами групп 1–8; карта ``SCENARIOS`` связывает каждый номер матрицы с его
тестом, а ``test_every_scenario_has_a_test`` не даёт карте разойтись с кодом.
"""

import importlib
from datetime import timedelta

import pandas as pd

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.lanes import CompletenessLane, EvidenceLane
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler
from tests.unit.data.lanes.test_lanes import SOURCE, Market, candles, minutes, run, setup, state

PACKAGE = "tests.unit.data.lanes"
SCENARIOS = {
    1: ("test_acceptance_matrix", "test_01_quiet_minute_is_confirmed_and_does_not_hold_decision"),
    2: ("test_acceptance_matrix", "test_02_traded_minute_without_candle_moves_to_completeness"),
    3: ("test_lanes", "test_known_delivery_loss_is_checked_without_waiting_publication_deadline"),
    4: ("test_lanes", "test_day_of_downtime_is_filled_by_a_few_range_requests"),
    5: ("test_lanes", "test_holes_outside_completeness_window_are_never_requested"),
    6: ("test_lanes", "test_minute_missing_at_source_gets_final_state_after_schedule"),
    7: ("test_source_absent_execution", "test_late_candle_flags_touched_stop_without_changing_facts"),
    8: ("test_request_arbiter", "test_waiting_live_lane_keeps_freed_slot_from_borrowers"),
    9: ("test_request_arbiter", "test_rate_limit_of_quotes_does_not_stop_instruments"),
    10: ("test_early_start", "test_live_launch_starts_market_data_before_instrument_selection"),
    11: ("test_acceptance_matrix", "test_11_restart_continues_from_saved_checks_without_rescan"),
    12: ("test_lanes", "test_obligation_minutes_go_first"),
    13: ("test_acceptance_matrix", "test_13_two_positions_first_then_pairs_in_turn"),
    14: ("test_store_lanes", "test_source_absent_is_exported_as_unresolved_and_resolves_prefix"),
    15: ("test_early_start", "test_stream_handoff_is_not_paced_by_global_window_with_lanes"),
    16: ("test_early_start", "test_saved_roster_is_registered_and_unselected_pairs_are_retired"),
    17: ("test_early_start", "test_history_launch_does_not_start_lanes"),
    18: ("test_lane_threads", "test_background_lanes_start_before_live_and_errors_are_isolated"),
}


def test_every_scenario_has_a_test():
    assert sorted(SCENARIOS) == list(range(1, 19))
    for module, name in SCENARIOS.values():
        assert callable(getattr(importlib.import_module(f"{PACKAGE}.{module}"), name, None)), (module, name)


def test_effective_profile_matches_design():
    profile = MarketDataSettings(independent_lanes=True)
    assert profile.lane_shares() == {"quotes": {"live": 50, "evidence": 25, "completeness": 25},
                                     "instruments": {"interactive": 50, "calendar": 50}}
    assert (profile.unary_requests_per_minute, profile.instrument_requests_per_minute) == (300, 150)
    assert profile.evidence_window_secs == 3300 and profile.evidence_retry_secs == (60, 180, 600)
    assert profile.completeness_recheck_secs == (3600, 86400)
    assert profile.completeness_rescan_interval_secs == 3600


def test_01_quiet_minute_is_confirmed_and_does_not_hold_decision(tmp_path):
    stored = [stamp for stamp in minutes("2026-10-13 17:00", "2026-10-13 17:15") if stamp != pd.Timestamp("2026-10-13 17:13")]
    market = Market({"1m": stored})
    env = setup(tmp_path, started="2026-10-13 16:59:30", now="2026-10-13 17:16:30",
                stored={"1m": [str(stamp) for stamp in stored]}, market=market)
    assert not env.cache.execution_ready(env.item, pd.Timestamp("2026-10-13 17:14"))

    assert env.evidence.step()

    assert state(env, "2026-10-13 17:13") == "confirmed_no_trade"
    assert len(market.calls) == 1 and len(market.trade_calls) == 1
    # Барьер 15m-решения, закрывшегося в 17:15, смотрит на 1m до 17:14.
    assert env.cache.execution_ready(env.item, pd.Timestamp("2026-10-13 17:14"))


def test_02_traded_minute_without_candle_moves_to_completeness(tmp_path):
    present = [stamp for stamp in minutes("2026-10-12 12:00", "2026-10-12 12:30") if stamp != pd.Timestamp("2026-10-12 12:04")]
    market = Market({"1m": present}, trades=["2026-10-12 12:04:20"])
    env = setup(tmp_path, started="2026-10-12 11:59:30", now="2026-10-12 12:10:05",
                stored={"1m": [str(stamp) for stamp in present if stamp <= pd.Timestamp("2026-10-12 12:08")]},
                market=market)
    assert env.worker.settings.evidence_retry_secs == (60, 180, 600)
    run(env.evidence)
    for delay in (61, 181, 601):
        env.clock[0] += timedelta(seconds=delay)
        run(env.evidence)
    assert state(env, "2026-10-12 12:04") not in {"confirmed_no_trade", "received", "source_absent"}
    evidence_calls = len(market.calls)
    assert evidence_calls >= 4                                             # первый запрос и короткие повторы

    # Минута вышла из окна доказательства: полоса доказательств её больше не трогает.
    env.clock[0] = pd.Timestamp("2026-10-12 13:00:05")
    run(env.evidence)
    assert all(start > pd.Timestamp("2026-10-12 12:04") for _, start, _ in market.calls[evidence_calls:])
    run(env.completeness)
    checks = env.store.window_snapshot(source=SOURCE, instrument_uid="uid", interval="1m",
                                       lower="2026-10-12 12:04", upper="2026-10-12 12:04")["checks"]
    # Короткие повторы не засчитаны: полнота начинает своё расписание с первой проверки.
    assert checks[pd.Timestamp("2026-10-12 12:04")]["checks"] == 1
    assert checks[pd.Timestamp("2026-10-12 12:04")]["next_check_at"] == pd.Timestamp("2026-10-12 14:00:05")


def test_11_restart_continues_from_saved_checks_without_rescan(tmp_path):
    present = [stamp for stamp in minutes("2026-10-12 10:00", "2026-10-12 10:30") if stamp != pd.Timestamp("2026-10-12 10:20")]
    market = Market({"1m": present})
    env = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 09:59"]}, market=market)
    run(env.completeness)
    assert state(env, "2026-10-12 10:19") == "received"
    before = len(market.calls)

    # Перезапуск: новый владелец, полосы и часы на той же рыночной базе.
    restarted = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:30:05",
                      stored={"1m": ["2026-10-12 09:59"]}, market=market)
    run(restarted.completeness)
    target = pd.Timestamp("2026-10-12 10:20")
    assert not any(start <= target < (end or pd.Timestamp.max) for _, start, end in market.calls[before:])
    assert not any(start < pd.Timestamp("2026-10-12 10:20") for _, start, _ in market.calls[before:])

    restarted.clock[0] = pd.Timestamp("2026-10-12 13:00:10")               # срок второй проверки
    restarted.completeness.wake()
    run(restarted.completeness)
    checks = restarted.store.window_snapshot(source=SOURCE, instrument_uid="uid", interval="1m",
                                             lower=target, upper=target)["checks"]
    assert checks[target]["checks"] == 2


def test_13_two_positions_first_then_pairs_in_turn(tmp_path):
    clock = [pd.Timestamp("2026-10-12 09:59:30")]
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    day = minutes("2026-10-12 10:00", "2026-10-12 11:00")
    calls = []

    def loader(**kwargs):
        calls.append(kwargs["ticker"])
        start, end = pd.Timestamp(kwargs["start_date"]), pd.Timestamp(kwargs["end_date"])
        return candles([stamp for stamp in day if start <= stamp < end]), f"uid-{kwargs['ticker']}"

    cache = MarketDataCache(loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0), market_store=store)
    tickers = [f"T{index:02d}" for index in range(12)]
    for ticker in tickers:
        uid = f"uid-{ticker}"
        store.record_candles(source=SOURCE, instrument_uid=uid, interval="1m",
                             candles=candles(["2026-10-12 09:58", "2026-10-12 09:59"]))
        cache.register_live_pair(Instrument(ticker, ticker, "future", ticker, uid), uid, "1m")
        cache.mark_live_history(uid, "1m", done=True)
    worker = IngestionWorker(cache, settings=MarketDataSettings(independent_lanes=True))
    worker.evidence = EvidenceLane(worker)
    completeness = CompletenessLane(worker)
    worker.set_execution_obligations({"uid-T07": (pd.Timestamp("2026-10-12 10:00"), True),
                                      "uid-T11": (pd.Timestamp("2026-10-12 10:00"), True)})
    clock[0] = pd.Timestamp("2026-10-12 12:00:05")

    for _ in range(len(tickers)):
        assert completeness.step()

    assert sorted(calls[:2]) == ["T07", "T11"]                            # позиции — первыми
    assert sorted(calls[2:]) == sorted(set(tickers) - {"T07", "T11"})     # каждая пара по разу, без монополии
