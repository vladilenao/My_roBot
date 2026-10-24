"""Старт ядра с повтором (add-history-contour): сбой не останавливает робота."""

from datetime import datetime, timezone

import pytest

from src.assembly import core
from src.logging_setup import register_secrets


class Core:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = []

    def start_market(self):
        self.calls.append("start")
        if self.failures:
            raise self.failures.pop(0)

    def reset_market(self):
        self.calls.append("reset")


def _starter(failures, *, token_required=False, delays=core.CORE_RETRY_DELAYS):
    sleeps, states = [], []
    target = Core(failures)
    starter = core.CoreStarter(target, token_required=token_required, delays=delays,
                               sleep=lambda secs: (sleeps.append(secs), states.append(starter.payload())),
                               clock=lambda: datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc))
    return starter, target, sleeps, states


def test_ready_on_first_attempt():
    starter, target, sleeps, _ = _starter([])
    assert starter.run() and starter.ready
    assert starter.payload() == {"state": "ready", "error": None, "attempts": 1, "next_retry_at": None}
    assert target.calls == ["start"] and sleeps == []


def test_pauses_grow_to_five_minutes_and_state_is_retrying():
    starter, target, sleeps, states = _starter([OSError("нет сети")] * 6)
    assert starter.run()
    assert sleeps == [30, 60, 120, 240, 300, 300]
    assert target.calls.count("reset") == 6 and target.calls[-1] == "start"
    assert states[0] == {"state": "retrying", "error": "OSError: нет сети", "attempts": 1,
                         "next_retry_at": "2026-10-24T09:00:30Z"}
    assert starter.payload()["attempts"] == 7


def test_error_text_hides_secrets():
    register_secrets(["fake-broker-token-for-test"])
    starter, _, _, states = _starter([RuntimeError("bad url https://x/?t=fake-broker-token-for-test")])
    starter.run()
    assert "fake-broker-token-for-test" not in states[0]["error"] and "[скрыто]" in states[0]["error"]


def test_without_broker_token_no_retry(monkeypatch, capsys):
    monkeypatch.setattr(core, "TINKOFF_TOKEN", "")
    starter, target, sleeps, _ = _starter([], token_required=True)
    assert starter.run() is False
    assert starter.payload()["state"] == "failed" and "TINKOFF_TOKEN" in starter.payload()["error"]
    assert target.calls == [] and sleeps == []
    assert "история работает" in capsys.readouterr().out


def test_snapshot_reports_starter_state():
    live = core.LiveCore.__new__(core.LiveCore)
    live.pulse = live.market_store = live.request_budget = None
    live.starter, _, _, _ = _starter([])
    snapshot = live.core_snapshot()
    assert snapshot["state"] == "starting" and snapshot["market_data"] is None and snapshot["quotas"] == {}


def test_reset_closes_partial_lanes():
    closed = []
    live = core.LiveCore.__new__(core.LiveCore)
    live.early = type("Early", (), {"poller": type("P", (), {"close": lambda self: closed.append(1)})()})()
    live.market_store = object()
    live.reset_market()
    assert closed == [1] and live.early is None and live.market_store is None


@pytest.mark.parametrize("delays", [(5,)])
def test_last_delay_repeats(delays):
    starter, _, sleeps, _ = _starter([OSError()] * 3, delays=delays)
    starter.run()
    assert sleeps == [5, 5, 5]
