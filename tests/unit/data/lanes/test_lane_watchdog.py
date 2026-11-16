"""Сторож полос (fix-live-lane-stall): тревога о зависшем шаге, сводка, снимок для API."""

import logging
import threading
import time
from types import SimpleNamespace

from src.data.lane_watchdog import LaneWatchdog
from src.data.poller import LiveMarketDataPoller
from src.data.request_budget import RequestBudget


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "src.data.lane_watchdog"]


def blocked_live_step(entered, release):
    entered.set()
    release.wait(5)


def test_stalled_step_warns_once_with_stack_then_recovers(caplog):
    clock, announced = [0.0], []
    watchdog = LaneWatchdog(300, clock=lambda: clock[0], announce=announced.append)
    entered, release = threading.Event(), threading.Event()

    def lane():
        watchdog.begin("live")
        try:
            blocked_live_step(entered, release)
        finally:
            watchdog.end("live")

    thread = threading.Thread(target=lane)
    thread.start()
    assert entered.wait(1)
    with caplog.at_level(logging.INFO, logger="src.data.lane_watchdog"):
        clock[0] = 299
        assert watchdog.check() == [] and watchdog.snapshot()["live"]["state"] == "ok"
        clock[0] = 301
        assert watchdog.check() == ["live"]
        assert watchdog.check() == []                          # повторной тревоги за тот же шаг нет
        warnings = _warnings(caplog)
        assert len(warnings) == 1 and "Полоса данных live не отвечает" in warnings[0]
        assert "blocked_live_step" in warnings[0]              # стек зависшего потока
        assert len(announced) == 1 and "live" in announced[0] and "blocked_live_step" not in announced[0]
        stalled = watchdog.snapshot()["live"]
        assert stalled["state"] == "stalled" and stalled["step_started_at"].endswith("Z")
        assert stalled["last_step_at"] is None
        clock[0] = 420
        release.set()
        thread.join(1)
    assert any("Полоса данных live снова работает: шаг длился 420 с" in record.getMessage()
               for record in caplog.records if record.levelno == logging.INFO)
    recovered = watchdog.snapshot()["live"]
    assert recovered["state"] == "ok" and recovered["step_started_at"] is None
    assert recovered["last_step_at"].endswith("Z")


def test_stack_contains_code_lines_not_locals(caplog):
    clock = [0.0]
    watchdog = LaneWatchdog(1, clock=lambda: clock[0], announce=lambda text: None)
    entered, release = threading.Event(), threading.Event()

    def lane():
        secret_value = "fake-token-for-watchdog-test"  # noqa: F841 — локальная переменная не должна попасть в стек
        watchdog.begin("evidence")
        try:
            blocked_live_step(entered, release)
        finally:
            watchdog.end("evidence")

    thread = threading.Thread(target=lane)
    thread.start()
    assert entered.wait(1)
    clock[0] = 2
    with caplog.at_level(logging.WARNING, logger="src.data.lane_watchdog"):
        watchdog.check()
    release.set()
    thread.join(1)
    text = _warnings(caplog)[0]
    assert "blocked_live_step" in text and "fake-token-for-watchdog-test" not in text


def test_idle_between_steps_is_not_a_stall(monkeypatch, caplog):
    monkeypatch.setattr(LiveMarketDataPoller, "COMPLETENESS_IDLE", 1.0)
    budget = RequestBudget(time.time, 0)
    steps = []
    lane = SimpleNamespace(name="completeness", step=lambda: steps.append(1) or False)
    poller = LiveMarketDataPoller(SimpleNamespace(_request_budget=budget), ["1m"], 0.05, lanes=[lane],
                                  stall_timeout=0.1)
    with caplog.at_level(logging.WARNING, logger="src.data.lane_watchdog"):
        poller.start_background()
        time.sleep(0.5)                                        # простой полосы полноты длиннее порога
        poller.close()
    assert steps and _warnings(caplog) == []
    assert poller.watchdog.snapshot()["completeness"]["state"] == "ok"


class StuckWorker:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def step(self):
        self.entered.set()
        self.release.wait(5)
        return False

    def next_wakeup_delay(self):
        return None

    def startup_complete(self):
        return True


def test_lane_summary_continues_while_live_step_is_stuck(caplog, capsys):
    budget = RequestBudget(time.time, 0)
    worker = StuckWorker()
    poller = LiveMarketDataPoller(SimpleNamespace(_request_budget=budget), ["1m"], 0.05, worker=worker,
                                  status_interval=0.05, stall_timeout=0.1)
    with caplog.at_level(logging.INFO, logger="src.data.poller"):
        poller.start()
        assert worker.entered.wait(1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("зависли: live" in record.getMessage() for record in caplog.records):
            time.sleep(0.05)
        summaries = [record.getMessage() for record in caplog.records if "Полосы данных: запросы" in record.getMessage()]
        assert any("зависли: live с " in text for text in summaries)
        assert poller.watchdog.snapshot()["live"]["state"] == "stalled"
        worker.release.set()
        poller.close()
    assert "Полоса данных live не отвечает" in capsys.readouterr().out
