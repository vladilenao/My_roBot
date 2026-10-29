"""Supervisor полос: ранний старт, изоляция ошибок, остановка, полоса в арбитре."""

import threading
import time
from types import SimpleNamespace

from src.data.poller import LiveMarketDataPoller
from src.data.request_budget import RequestBudget


class Lane:
    def __init__(self, name, budget, fail=False):
        self.name, self.budget, self.fail = name, budget, fail
        self.steps, self.lanes = 0, set()
        self.started = threading.Event()

    def step(self):
        self.steps += 1
        self.started.set()
        self.lanes.add(self.budget.current_lane())
        if self.fail:
            raise RuntimeError("сбой разбора")
        return False


class Worker:
    def __init__(self):
        self.steps = 0

    def step(self):
        self.steps += 1
        return False

    def next_wakeup_delay(self):
        return None

    def startup_complete(self):
        return True


def test_background_lanes_start_before_live_and_errors_are_isolated(monkeypatch):
    monkeypatch.setattr(LiveMarketDataPoller, "LANE_ERROR_PAUSE", 0.05)
    budget = RequestBudget(time.time, 0)
    cache = SimpleNamespace(_request_budget=budget)
    broken, healthy = Lane("completeness", budget, fail=True), Lane("evidence", budget)
    worker = Worker()
    poller = LiveMarketDataPoller(cache, ["1m"], 0.05, worker=worker, lanes=[broken, healthy])
    poller.start_background()
    assert healthy.started.wait(1) and broken.started.wait(1)
    time.sleep(0.2)
    assert worker.steps == 0                                  # живая полоса ещё не запущена
    assert healthy.steps > 1 and broken.steps > 1             # сбой одной полосы не остановил другие
    poller.start()
    time.sleep(0.1)
    assert worker.steps > 0
    poller.close()
    assert all(not thread.is_alive() for thread in poller._threads)
    assert healthy.lanes == {"evidence"} and broken.lanes == {"completeness"}
