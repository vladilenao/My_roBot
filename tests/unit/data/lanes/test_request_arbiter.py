"""Арбитр лимитов: сервисы раздельно, доли полос, приоритет при конкуренции."""

import threading
from datetime import datetime, timedelta

import pytest

from src.data.request_budget import RequestBudget, RequestDeferred

T0 = datetime(2026, 10, 13, 12)
SHARES = {"quotes": {"live": 50, "evidence": 25, "completeness": 25}}


def arbiter(clock, ceiling=4):
    return RequestBudget(lambda: clock[0], 0, per_minute=ceiling, lane_shares=SHARES)


def call(budget, lane, key=None, service=None):
    return budget.call(lambda: "ok", key=key, lane=lane, service=service)


def test_lane_borrows_idle_quota_beyond_its_share():
    clock = [T0]
    budget = arbiter(clock)
    for index in range(4):  # гарантия полноты — 1, остальное занято взаймы
        assert call(budget, "completeness", key=index) == "ok"
    with pytest.raises(RequestDeferred):
        call(budget, "completeness", key="over")
    assert budget.snapshot()["quotes"]["lanes"] == {"completeness": 4}


def test_waiting_live_lane_keeps_freed_slot_from_borrowers():
    clock = [T0]
    budget = arbiter(clock)
    call(budget, "completeness", key=1)                 # гарантия
    clock[0] = T0 + timedelta(seconds=1)
    call(budget, "completeness", key=2)  # взаймы
    clock[0] = T0 + timedelta(seconds=2)
    call(budget, "evidence", key=3)
    clock[0] = T0 + timedelta(seconds=3)
    call(budget, "live", key=4)          # окно заполнено
    clock[0] = T0 + timedelta(seconds=59.5)
    with pytest.raises(RequestDeferred):
        call(budget, "live", key=5)                      # живая ждёт квоты
    clock[0] = T0 + timedelta(seconds=60.5)              # освободилось одно место
    with pytest.raises(RequestDeferred):
        call(budget, "completeness", key=6)              # не занимает его взаймы
    clock[0] = T0 + timedelta(seconds=60.6)
    assert call(budget, "live", key=5) == "ok"


def test_guaranteed_share_is_available_even_when_others_borrowed():
    clock = [T0]
    budget = arbiter(clock, ceiling=8)
    for index in range(6):
        call(budget, "completeness", key=index)          # 2 своих + 4 взаймы
    assert call(budget, "live", key="a") == "ok"
    assert call(budget, "live", key="b") == "ok"         # окно 8 заполнено, но гарантия живой — 4
    with pytest.raises(RequestDeferred):
        call(budget, "live", key="c")


def test_rate_limit_of_quotes_does_not_stop_instruments():
    clock = [T0]
    budget = RequestBudget(lambda: clock[0], 0, per_minute_by_service={"instruments": 10})

    def exhausted():
        raise RuntimeError("RESOURCE_EXHAUSTED ratelimit_reset=30")

    with pytest.raises(RuntimeError):
        budget.call(exhausted, key="candles")
    with pytest.raises(RequestDeferred):
        budget.call(lambda: None, key="other-candles")
    assert budget.call(lambda: "schedule", key="schedule", service="instruments", lane="calendar") == "schedule"
    assert budget.retry_after_for("instruments") is None
    clock[0] += timedelta(seconds=30)
    assert budget.call(lambda: "ok", key="other-candles") == "ok"


def test_lane_comes_from_thread_context():
    clock = [T0]
    budget = arbiter(clock, ceiling=100)
    with budget.lane("evidence"):
        budget.call(lambda: None, key="in-evidence")
    seen = []
    worker = threading.Thread(target=lambda: seen.append(budget.current_lane()))
    with budget.lane("completeness"):
        worker.start()
        worker.join()
        budget.call(lambda: None, key="in-completeness")
    assert seen == ["live"]  # контекст полосы не утекает в другой поток
    assert budget.calls_by_lane == {("quotes", "evidence"): 1, ("quotes", "completeness"): 1}


def test_zero_interval_has_no_global_pacing_window():
    clock = [T0]
    budget = RequestBudget(lambda: clock[0], 0, per_minute=600)
    assert [budget.call(lambda: index, key=index) for index in range(5)] == list(range(5))


def test_shares_are_validated():
    with pytest.raises(ValueError):
        RequestBudget(lambda: T0, 0, lane_shares={"quotes": {"live": 80, "completeness": 40}})


def test_wrapped_client_routes_services_and_waits_for_quota():
    from types import SimpleNamespace

    clock = [T0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    budget = RequestBudget(lambda: clock[0], 0, sleep=sleep, per_minute_by_service={"instruments": 1})
    raw = SimpleNamespace(instruments=SimpleNamespace(shares=lambda **_: "shares", lot=1),
                          market_data=SimpleNamespace(get_candles=lambda **_: "candles"),
                          get_all_candles=lambda **_: iter(()))
    client = budget.wrap_client(raw, lane="interactive")
    assert client.instruments.shares() == "shares"
    assert client.instruments.shares() == "shares"         # квота 1/мин — дождались окна
    assert sleeps and sleeps[0] == pytest.approx(60)
    assert client.market_data.get_candles() == "candles"
    assert client.instruments.lot == 1 and list(client.get_all_candles()) == []
    assert budget.calls_by_lane == {("instruments", "interactive"): 2, ("quotes", "interactive"): 1}


def test_calls_without_key_are_not_deduplicated_across_threads():
    clock = [T0]
    budget = RequestBudget(lambda: clock[0], 0)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(2)
        return "slow"

    worker = threading.Thread(target=lambda: budget.call(slow))
    worker.start()
    assert entered.wait(2)
    assert budget.call(lambda: "fast") == "fast"           # второй вызов без ключа не откладывается
    release.set()
    worker.join(2)
