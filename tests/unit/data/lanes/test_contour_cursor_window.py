"""Залипание 1m-исполнения контура без сделок на неразрешённой минуте (23.10.2026, BR-11.26).

Сделка закрылась — обязательств нет, граница исполнения прежней сделки осталась
у пары и сняла нижнюю границу запуска. Окно полноты 1m сжалось до последнего
бара, а минута, пропущенная оборванным потоком, осталась ``pending`` позади
окна: ни полоса доказательств, ни полоса полноты её не проверяли, контур
стоял перед ней до конца сессии.
"""

from datetime import timedelta

import pandas as pd

from src.data.cache import MarketDataCache

from tests.unit.data.lanes.test_lanes import SOURCE, Market, minutes, run, setup, state

CONTOUR = "emulation"
STUCK = pd.Timestamp("2026-10-23 17:45")


def stalled(tmp_path, *, cursor="2026-10-23 17:44", now="2026-10-23 18:51:05"):
    end = pd.Timestamp(now).floor("1min") - pd.Timedelta(minutes=1)
    present = [stamp for stamp in minutes("2026-10-23 17:30", end) if stamp != STUCK]
    market = Market({"1m": present})
    env = setup(tmp_path, started="2026-10-23 06:51:30", now=now,
                stored={"1m": [str(stamp) for stamp in present]}, market=market)
    # Граница исполнения сделки, закрытой днём, — у пары навсегда (min), обязательств уже нет.
    env.cache.set_live_execution_lower("uid", "1m", pd.Timestamp("2026-10-23 10:00"))
    env.store.set_interval_states(source=SOURCE, instrument_uid="uid", interval="1m",
                                  rows=[(STUCK, "pending", "applicable_trading_schedule:test")],
                                  observed_at=pd.Timestamp("2026-10-23 17:49:08"))
    if cursor is not None:
        env.store.mark_processed(source=SOURCE, instrument_uid="uid", interval="1m",
                                 open_time=pd.Timestamp(cursor), contour=CONTOUR)
    return env, market


def test_window_reaches_contour_cursor_without_obligations(tmp_path):
    env, _ = stalled(tmp_path)
    pair = env.cache.live_pair("uid", "1m")
    assert env.worker.execution_boundary("uid") is None
    assert env.completeness.window_lower(pair, env.clock[0]) <= STUCK


def test_recent_stuck_minute_gets_evidence_check(tmp_path):
    env, market = stalled(tmp_path, now="2026-10-23 17:52:05")
    assert env.evidence.step()
    assert any(start <= STUCK < (end or pd.Timestamp.max) for _, start, end in market.calls)
    assert state(env, STUCK) == "confirmed_no_trade"                       # сделок в минуте не было


def test_stuck_minute_behind_window_gets_final_state(tmp_path):
    env, market = stalled(tmp_path)
    run(env.completeness)
    assert any(start <= STUCK < (end or pd.Timestamp.max) for _, start, end in market.calls)
    for delay in (timedelta(hours=1), timedelta(hours=24)):
        env.clock[0] += delay + timedelta(seconds=1)
        env.completeness.wake()
        run(env.completeness)
    assert state(env, STUCK) == "source_absent"
    # Барьер исполнения контура проходит минуту «у источника нет».
    fresh = env.cache.frame_for(env.item, "1m")
    fresh = fresh[fresh["datetime"] > pd.Timestamp("2026-10-23 17:44")]
    passed = MarketDataCache.contiguous_after(env.cache, env.item, "1m", fresh, pd.Timestamp("2026-10-23 17:44"))
    assert pd.Timestamp("2026-10-23 17:46") in set(passed["datetime"])


def test_window_without_contour_cursor_stays_narrow(tmp_path):
    env, market = stalled(tmp_path, cursor=None)
    pair = env.cache.live_pair("uid", "1m")
    assert env.completeness.window_lower(pair, env.clock[0]) > STUCK
    run(env.completeness)
    assert not any(start <= STUCK < (end or pd.Timestamp.max) for _, start, end in market.calls)
