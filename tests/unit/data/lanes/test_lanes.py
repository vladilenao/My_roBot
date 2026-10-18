"""Полосы доказательств и полноты на управляемых часах и фиктивной бирже."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
from t_tech.invest.schemas import TradeSourceType

from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.lanes import CHART_BARS, CompletenessLane, EvidenceLane
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler

SOURCE = "tbank_exchange"


def candles(stamps):
    stamps = [pd.Timestamp(item) for item in stamps]
    return pd.DataFrame({"datetime": pd.to_datetime(stamps), "open": [100.0] * len(stamps),
                         "high": [101.0] * len(stamps), "low": [99.0] * len(stamps), "close": [100.0] * len(stamps),
                         "volume": [1] * len(stamps), "is_complete": [True] * len(stamps)})


def minutes(start, end):
    return list(pd.date_range(start, end, freq="1min"))


class Market:
    """Фиктивная биржа: свечи по ТФ, сделки, журнал запросов, отказ по флагу."""

    def __init__(self, bars, trades=()):
        self.bars = {tf: [pd.Timestamp(item) for item in items] for tf, items in bars.items()}
        self.trades = [pd.Timestamp(item) for item in trades]
        self.calls, self.trade_calls, self.fail = [], [], False

    def loader(self, **kwargs):
        self.calls.append((kwargs["timeframe"], pd.Timestamp(kwargs["start_date"]), kwargs.get("end_date")))
        if self.fail:
            raise RuntimeError("UNAVAILABLE")
        start, end = pd.Timestamp(kwargs["start_date"]), kwargs.get("end_date")
        rows = [item for item in self.bars.get(kwargs["timeframe"], ())
                if item >= start and (end is None or item < pd.Timestamp(end))]
        return candles(rows), "uid"

    def client_context(self, token=None):
        market = self

        @contextmanager
        def context():
            def get_last_trades(*, instrument_id, from_, to, trade_source):
                assert trade_source == TradeSourceType.TRADE_SOURCE_ALL
                lower, upper = pd.Timestamp(from_).tz_convert(None), pd.Timestamp(to).tz_convert(None)
                market.trade_calls.append((lower, upper))
                return SimpleNamespace(trades=[SimpleNamespace(time=stamp.tz_localize("UTC").to_pydatetime())
                                               for stamp in market.trades if lower <= stamp < upper])
            yield SimpleNamespace(market_data=SimpleNamespace(get_last_trades=get_last_trades))
        return context()


def setup(tmp_path, *, started, now, stored, market, timeframes=("1m",), settings=None):
    clock = [pd.Timestamp(started)]
    timeline = MultiTimeframeScheduler(sorted(set(timeframes) | {"1m"}), clock=lambda: clock[0].to_pydatetime())
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache = MarketDataCache(market.loader, timeline, request_budget=RequestBudget(lambda: clock[0], 0),
                            market_store=store, client_provider=market)
    item = Instrument("NGZ6", "NGZ6", "future", "NGZ6", "uid")
    for timeframe in timeframes:
        store.record_candles(source=SOURCE, instrument_uid="uid", interval=timeframe, candles=candles(stored[timeframe]))
        cache.register_live_pair(item, "uid", timeframe)
        cache.mark_live_history("uid", timeframe, done=True)
    worker = IngestionWorker(cache, settings=settings or MarketDataSettings(independent_lanes=True))
    completeness = CompletenessLane(worker)
    evidence = EvidenceLane(worker, completeness)
    worker.evidence = evidence
    worker.completeness = completeness
    clock[0] = pd.Timestamp(now)
    return SimpleNamespace(clock=clock, store=store, cache=cache, worker=worker, evidence=evidence,
                           completeness=completeness, item=item)


def state(env, stamp, timeframe="1m"):
    return env.store.coverage_state(source=SOURCE, instrument_uid="uid", interval=timeframe, open_time=stamp)


def run(lane, limit=50):
    for _ in range(limit):
        if not lane.step():
            return


# ── полоса доказательств ──

def test_quiet_minutes_are_confirmed_and_traded_minute_without_candle_waits(tmp_path):
    market = Market({"1m": ["2026-10-12 12:00", "2026-10-12 12:06"]}, trades=["2026-10-12 12:04:20", "2026-10-12 12:06:05"])
    env = setup(tmp_path, started="2026-10-12 12:00:30", now="2026-10-12 12:10:05",
                stored={"1m": ["2026-10-12 12:00"]}, market=market)
    assert env.evidence.step()
    assert len(market.calls) == 1 and len(market.trade_calls) == 1        # один GetCandles + один GetLastTrades
    assert [state(env, f"2026-10-12 12:0{m}") for m in (1, 2, 3, 5, 7, 8)] == ["confirmed_no_trade"] * 6
    assert state(env, "2026-10-12 12:04") != "confirmed_no_trade"         # сделка была, свечи нет
    assert not env.evidence.step()                                         # повтор не раньше короткой паузы
    env.clock[0] += timedelta(seconds=61)
    assert env.evidence.step() and len(market.calls) == 2


def test_known_delivery_loss_is_checked_without_waiting_publication_deadline(tmp_path):
    market = Market({"1m": minutes("2026-10-12 12:00", "2026-10-12 12:06")})
    env = setup(tmp_path, started="2026-10-12 12:00:30", now="2026-10-12 12:07:10",
                stored={"1m": ["2026-10-12 12:00", "2026-10-12 12:01"]}, market=market)
    env.worker.note_loss("uid", "1m", pd.Timestamp("2026-10-12 12:02"))
    assert env.evidence.step()
    assert all(state(env, stamp) == "received" for stamp in minutes("2026-10-12 12:02", "2026-10-12 12:06"))


def test_evidence_lane_ignores_minutes_older_than_its_window(tmp_path):
    market = Market({"1m": ["2026-10-12 10:00"]})
    env = setup(tmp_path, started="2026-10-12 10:00:30", now="2026-10-12 12:10:05",
                stored={"1m": ["2026-10-12 10:00"] + [str(m) for m in minutes("2026-10-12 11:20", "2026-10-12 12:08")]},
                market=market)
    assert not env.evidence.step() or all(start >= pd.Timestamp("2026-10-12 11:15") for _, start, _ in market.calls)


# ── полоса полноты ──

def test_day_of_downtime_is_filled_by_a_few_range_requests(tmp_path):
    day = minutes("2026-10-12 07:00", "2026-10-12 20:59")
    hours = list(pd.date_range("2026-10-08 07:00", "2026-10-12 20:00", freq="1h"))
    market = Market({"1m": day, "1h": hours})
    stored_hours = [stamp for stamp in hours if stamp < pd.Timestamp("2026-10-12 07:00")]
    env = setup(tmp_path, started="2026-10-12 06:00:30", now="2026-10-13 06:00:05",
                stored={"1m": [str(day[0] - timedelta(minutes=1))], "1h": [str(s) for s in stored_hours]},
                market=market, timeframes=("1m", "1h"))
    run(env.completeness)
    frame = env.cache.frame_for(env.item, "1h")
    assert set(hours[-CHART_BARS:]) <= set(frame["datetime"])            # 80 часовых баров картинки целые
    assert set(day) <= set(env.cache.frame_for(env.item, "1m")["datetime"])
    assert len(market.calls) <= 4                                           # единицы запросов, а не сотни


def test_minute_missing_at_source_gets_final_state_after_schedule(tmp_path):
    present = [stamp for stamp in minutes("2026-10-12 10:00", "2026-10-12 10:30") if stamp != pd.Timestamp("2026-10-12 10:20")]
    market = Market({"1m": present})
    env = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 09:59"]}, market=market)
    env.cache.register_live_pair(env.item, "uid", "1m")
    run(env.completeness)
    assert state(env, "2026-10-12 10:20") not in {"source_absent", "received"}
    for delay in (timedelta(hours=1), timedelta(hours=24)):
        env.clock[0] += delay + timedelta(seconds=1)
        env.completeness.wake()
        run(env.completeness)
    assert state(env, "2026-10-12 10:20") == "source_absent"
    status = [row["interval_status"] for row in env.store.changes_after(env.store.producer_id, 0, limit=1000)["changes"]
              if row["kind"] == "interval_status" and row["interval_status"]["open_time_utc"].startswith("2026-10-12T10:20")]
    assert status[-1]["state"] == "unresolved" and status[-1]["evidence"].startswith("source_absent:checks=3")
    finalized = len(market.calls)
    env.clock[0] += timedelta(hours=30)
    env.completeness.wake()
    run(env.completeness)
    target = pd.Timestamp("2026-10-12 10:20")
    later = market.calls[finalized:]
    # Итог окончателен: минуту 10:20 больше не запрашивают (без календаря новые
    # минуты окна после 10:30 проходят своё расписание, это правильно).
    assert not any(start <= target < (end or pd.Timestamp.max) for _, start, end in later)


def test_holes_outside_completeness_window_are_never_requested(tmp_path):
    hours = list(pd.date_range("2026-09-01 07:00", "2026-10-12 20:00", freq="1h"))
    stored = [stamp for stamp in hours if not pd.Timestamp("2026-09-11 00:00") <= stamp < pd.Timestamp("2026-09-12 00:00")]
    market = Market({"1h": hours})
    env = setup(tmp_path, started="2026-10-12 20:30", now="2026-10-12 21:30:05",
                stored={"1m": ["2026-10-12 20:29"], "1h": [str(s) for s in stored]}, market=market,
                timeframes=("1m", "1h"))
    run(env.completeness)
    assert all(start >= pd.Timestamp("2026-10-01") for tf, start, _ in market.calls if tf == "1h")


def test_obligation_minutes_go_first(tmp_path):
    market = Market({"1m": minutes("2026-10-12 08:00", "2026-10-12 11:00"),
                     "1h": list(pd.date_range("2026-10-05 07:00", "2026-10-12 10:00", freq="1h"))})
    env = setup(tmp_path, started="2026-10-12 07:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 07:59"], "1h": ["2026-10-05 07:00"]}, market=market,
                timeframes=("1m", "1h"))
    env.worker.set_execution_obligations({"uid": (pd.Timestamp("2026-10-12 08:30"), True)})  # ключ — UID пары
    assert env.completeness.step()
    assert market.calls[0][0] == "1m" and market.calls[0][1] <= pd.Timestamp("2026-10-12 08:30")


def test_source_error_is_not_counted_as_empty_check(tmp_path):
    market = Market({"1m": minutes("2026-10-12 10:00", "2026-10-12 10:30")})
    env = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 09:59"]}, market=market)
    market.fail = True
    assert env.completeness.step()
    snapshot = env.store.window_snapshot(source=SOURCE, instrument_uid="uid", interval="1m",
                                         lower="2026-10-12 10:00", upper="2026-10-12 11:00")
    assert snapshot["checks"] == {}
    market.fail = False
    env.clock[0] += timedelta(seconds=61)
    run(env.completeness)
    assert state(env, "2026-10-12 10:20") == "received"


def test_completeness_window_covers_chart_and_obligation(tmp_path):
    market = Market({})
    hours = [str(s) for s in pd.date_range("2026-10-01 07:00", "2026-10-12 20:00", freq="1h")]
    env = setup(tmp_path, started="2026-10-12 20:30", now="2026-10-12 21:30:05",
                stored={"1m": ["2026-10-12 20:29"], "1h": hours}, market=market, timeframes=("1m", "1h"))
    pair_1h = env.cache.live_pair("uid", "1h")
    assert env.completeness.window_lower(pair_1h, env.clock[0]) == pd.Timestamp(hours[-CHART_BARS])
    env.worker.set_execution_obligations({"uid": (pd.Timestamp("2026-10-12 15:00"), False)})
    pair_1m = env.cache.live_pair("uid", "1m")
    assert env.completeness.window_lower(pair_1m, env.clock[0]) == pd.Timestamp("2026-10-12 15:01")
    roster_window = env.store.roster(source=SOURCE)
    assert roster_window == []  # окно пишется в состав только для сохранённых пар


def test_saved_recovery_book_is_adopted_once_and_removed(tmp_path):
    from src.data.ingestion import RecoveryRange

    day = minutes("2026-10-12 12:00", "2026-10-12 12:30")
    market = Market({"1m": day})
    env = setup(tmp_path, started="2026-10-12 12:00:30", now="2026-10-12 14:00:05",
                stored={"1m": [str(stamp) for stamp in day if not "12:05" <= stamp.strftime("%H:%M") <= "12:09"]},
                market=market)
    far = pd.Timestamp("2026-10-20")
    book = RecoveryRange(pd.Timestamp("2026-10-12 12:05"), pd.Timestamp("2026-10-12 12:09"), far, far,
                         pd.Timestamp("2026-10-12 12:05"), attempts=5, state="parked")
    env.store.save_live_state(book.as_json(), kind="recovery", source=SOURCE, instrument_uid="uid", interval="1m")
    env.store.save_live_state(book.as_json(), kind="recovery", source="other", instrument_uid="uid", interval="1m")

    run(env.completeness)

    # Парковка книги не задерживает догрузку: дыра — обычная дыра окна.
    assert all(state(env, stamp) == "received" for stamp in minutes("2026-10-12 12:05", "2026-10-12 12:09"))
    assert env.store.live_state(kind="recovery", source=SOURCE, instrument_uid="uid", interval="1m") == {}
    assert env.store.live_state(kind="recovery", source="other", instrument_uid="uid", interval="1m") != {}
    assert env.completeness.adopt_recovery_books() == []


def test_lane_diagnostics_are_machine_readable_in_pair_status(tmp_path):
    present = [stamp for stamp in minutes("2026-10-12 10:00", "2026-10-12 10:30") if stamp != pd.Timestamp("2026-10-12 10:20")]
    market = Market({"1m": present})
    env = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 09:59"]}, market=market)
    run(env.completeness)
    env.completeness.holes(env.cache.live_pair("uid", "1m"), pd.Timestamp(env.clock[0]))
    lanes = env.cache.pair_status(env.item, "1m")["lanes"]
    completeness = lanes["completeness"]
    assert completeness["holes_due"] == 0 and completeness["source_absent"] == 0
    assert completeness["next_check_at"] == "2026-10-12T13:00:05Z"     # первая повторная проверка 10:20
    assert completeness["window_lower"] <= "2026-10-12T10:00:00Z"

    for delay in (timedelta(hours=1), timedelta(hours=24)):
        env.clock[0] += delay + timedelta(seconds=1)
        env.completeness.wake()
        run(env.completeness)
    env.completeness.holes(env.cache.live_pair("uid", "1m"), pd.Timestamp(env.clock[0]))
    assert env.cache.pair_status(env.item, "1m")["lanes"]["completeness"]["source_absent"] >= 1


def test_evidence_lane_reports_pending_recent_minute(tmp_path):
    market = Market({"1m": ["2026-10-12 12:00", "2026-10-12 12:06"]}, trades=["2026-10-12 12:04:20"])
    env = setup(tmp_path, started="2026-10-12 12:00:30", now="2026-10-12 12:10:05",
                stored={"1m": ["2026-10-12 12:00"]}, market=market)
    run(env.evidence)
    evidence = env.cache.pair_status(env.item, "1m")["lanes"]["evidence"]
    assert evidence["missing"] == "2026-10-12T12:04:00Z" and evidence["attempts"] >= 1
    assert evidence["next_check_at"] is not None


def test_evidence_covers_needed_minutes_before_process_start(tmp_path):
    # Перезапуск посреди сессии: тихая минута и недогруженная свеча за полчаса
    # до старта ещё доказуемы GetLastTrades и нужны окну прогрева пары.
    market = Market({"1m": [stamp for stamp in minutes("2026-10-12 12:00", "2026-10-12 12:34")
                            if stamp != pd.Timestamp("2026-10-12 12:05")]})
    stored = [str(stamp) for stamp in minutes("2026-10-12 12:00", "2026-10-12 12:34")
              if stamp not in (pd.Timestamp("2026-10-12 12:05"), pd.Timestamp("2026-10-12 12:10"))]
    env = setup(tmp_path, started="2026-10-12 12:30:30", now="2026-10-12 12:35:30", stored={"1m": stored},
                market=market)
    env.cache.register_live_pair(env.item, "uid", "1m", 40)
    env.cache.mark_live_history("uid", "1m", done=True)
    run(env.evidence)
    assert state(env, "2026-10-12 12:05") == "confirmed_no_trade"
    assert state(env, "2026-10-12 12:10") == "received"


def test_restart_with_unfinished_trade_resumes_check_schedule(tmp_path):
    """14.10: минута ED 14:06 после двух проверок выпала из окна перезапуска (fix-live-pair-identity)."""
    missing = pd.Timestamp("2026-10-13 14:06")
    market = Market({"1m": [stamp for stamp in minutes("2026-10-13 14:00", "2026-10-14 06:00") if stamp != missing]})
    before = setup(tmp_path, started="2026-10-13 13:59:30", now="2026-10-13 15:05:05",
                   stored={"1m": ["2026-10-13 13:59"]}, market=market)
    run(before.completeness)
    before.clock[0] += timedelta(hours=1, seconds=1)
    before.completeness.wake()
    run(before.completeness)
    def checks(env):
        return env.store.window_snapshot(source=SOURCE, instrument_uid="uid", interval="1m",
                                                       lower=missing, upper=missing)["checks"][missing]
    assert checks(before)["checks"] == 2

    # Перезапуск: полосы стартуют до выбора, обязательств ещё нет — окно от границы запуска.
    after = setup(tmp_path, started="2026-10-14 04:22:30", now="2026-10-14 04:23:05", stored={"1m": []}, market=market)
    pair = after.cache.live_pair("uid", "1m")
    run(after.completeness)
    assert after.completeness.window_lower(pair, after.clock[0]) > missing
    assert after.completeness._level(pair) == 3

    after.worker.set_execution_obligations({"uid": (pd.Timestamp("2026-10-13 14:00"), True)})
    assert after.completeness._skip_until == {}                       # изменение обязательств будит полосу
    assert after.completeness.window_lower(pair, after.clock[0]) <= pd.Timestamp("2026-10-13 14:00")
    assert after.completeness._level(pair) == 0
    due, later = after.completeness.holes(pair, after.clock[0])
    assert missing not in due and later == checks(after)["next_check_at"]   # счётчик не начат заново

    after.clock[0] = pd.Timestamp(checks(after)["next_check_at"]) + timedelta(seconds=1)
    after.completeness.wake()
    run(after.completeness)
    assert state(after, missing) == "source_absent"
    assert checks(after)["checks"] == 3


def test_unchanged_obligations_do_not_wake_completeness(tmp_path):
    env = setup(tmp_path, started="2026-10-12 09:59:30", now="2026-10-12 12:00:05",
                stored={"1m": ["2026-10-12 09:59"]}, market=Market({}))
    env.worker.set_execution_obligations({"uid": (pd.Timestamp("2026-10-12 08:30"), True)})
    env.completeness._skip_until[("uid", "1m")] = env.clock[0] + timedelta(hours=1)
    env.worker.set_execution_obligations({"uid": (pd.Timestamp("2026-10-12 08:30"), True)})   # каждый тик
    assert ("uid", "1m") in env.completeness._skip_until
