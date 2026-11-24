"""Плановая недоступность брокера (add-finam-contour, решения 7–8; tasks 2.6).

``BrokerOutage`` — брокер в окне обслуживания: допуск ждёт конца окна без тревоги, сверка пропускается без
счёта пропусков, ``account_link`` — ``maintenance``. Обычный ``BrokerCallError`` — как раньше.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.broker_contour import process as process_module
from src.broker_contour.port import AccountSnapshot, BrokerCallError, BrokerOutage, RobotAccount, TradingSession
from src.broker_contour.process import BrokerContour
from src.broker_contour.reconcile import Reconciler
from src.broker_contour.store import BrokerStore
from src.events.bus import EventBus
from tests.unit.broker_contour.test_broker_contour import Collector, FakeNotifier, Stop, _settings

NOW = datetime(2026, 11, 25, 2, 30, tzinfo=timezone.utc)      # 05:30 МСК


class ReadOnlySession:
    """Сессия только на чтение: ``outages`` допусков подряд в окне, затем снимок; ``reads`` — исходы чтений."""

    def __init__(self, stop, *, outages=0, reads=(), stop_after=1):
        self.stop, self.outages, self.reads, self.stop_after = stop, outages, list(reads), stop_after
        self.admissions, self.read_calls, self.statuses = 0, 0, []
        self.directory = None

    def find_account(self) -> RobotAccount:
        self.admissions += 1
        if self.directory is not None and (self.directory / "status.json").exists():
            self.statuses.append(json.loads((self.directory / "status.json").read_text(encoding="utf-8")))
        if self.outages:
            self.outages -= 1
            raise BrokerOutage("auth.TokenDetails", "UNAVAILABLE", message="окно обслуживания")
        return RobotAccount("acc-finam-00007777", opened=False, access_level="read_only")

    def read_facts(self, account):
        return None

    def read_account(self, account, names=None) -> AccountSnapshot:
        self.read_calls += 1
        outcome = self.reads.pop(0) if self.reads else None
        if self.read_calls > self.stop_after:
            self.stop.request("тест")
        if outcome is not None:
            raise outcome
        return AccountSnapshot(Decimal(0), None)

    def fee_probes(self, path):
        return None


def _factory(session):
    @contextmanager
    def open_session():
        yield session
    return open_session


def _contour(tmp_path, session, stop):
    settings = _settings(tmp_path, contour_id="finam", kind="account", broker="finam", provider="finam",
                         directory=tmp_path / "contours" / "finam", token_env="FINAM_SECRET_TOKEN",
                         account_id="acc-finam-00007777", account_id_env="FINAM_ACCOUNT_ID")
    session.directory = settings.directory
    bus, collector = EventBus(), Collector()
    bus.subscribe_all([collector])
    return BrokerContour(settings, session_factory=_factory(session), bus=bus, stop=stop, pid=1), collector


def _status(tmp_path) -> dict:
    return json.loads((tmp_path / "contours" / "finam" / "status.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch):
    monkeypatch.setattr(process_module, "OUTAGE_RETRY_SECS", 0.01)


def test_admission_waits_for_window_without_alarm(tmp_path):
    stop = Stop()
    session = ReadOnlySession(stop, outages=2)
    contour, collector = _contour(tmp_path, session, stop)
    assert contour.run() == 0
    assert session.admissions == 3
    waiting = session.statuses[-1]                  # статус перед третьим допуском — после второго окна
    assert (waiting["state"], waiting["reason"], waiting["account_link"]) == ("starting", "окно обслуживания брокера",
                                                                              "maintenance")
    assert "refused" not in collector.kinds() and "started" in collector.kinds()
    status = _status(tmp_path)
    assert (status["state"], status["broker"], status["kind"]) == ("stopped", "finam", "account")


def test_stop_while_waiting_for_window(tmp_path):
    stop = Stop()
    session = ReadOnlySession(stop, outages=100)

    original = session.find_account

    def find_and_stop():
        if session.admissions == 1:
            stop.request("остановка командой (no_entries)")
        return original()

    session.find_account = find_and_stop
    contour, collector = _contour(tmp_path, session, stop)
    assert contour.run() == 0
    assert session.admissions == 2
    status = _status(tmp_path)
    assert (status["state"], status["exit_reason"]) == ("stopped", "остановка командой (no_entries)")
    assert collector.kinds() == []                  # ни тревоги, ни карточки старта


def test_reconcile_in_window_keeps_link_maintenance(tmp_path):
    stop = Stop()
    outage = BrokerOutage("accounts.GetAccount", "UNAVAILABLE")
    session = ReadOnlySession(stop, reads=[None] + [outage] * 5, stop_after=4)   # и снимок при остановке
    contour, collector = _contour(tmp_path, session, stop)
    assert contour.run() == 0
    assert "alarm" not in collector.kinds()
    assert _status(tmp_path)["account_link"] == "maintenance"


def test_reconciler_planned_skip_is_not_counted(tmp_path):
    notifier = FakeNotifier()
    reconciler = Reconciler(store=BrokerStore(tmp_path / "broker.sqlite3"), notifier=notifier, account_tail="…7777",
                            every_secs=60)
    for _ in range(5):
        report = reconciler.run(NOW, None, planned=True)
    assert (report["result"], report["skipped_in_row"]) == ("skipped", 0) and notifier.calls == []


def test_reconciler_lost_account_still_alarms_on_third(tmp_path):
    notifier = FakeNotifier()
    reconciler = Reconciler(store=BrokerStore(tmp_path / "broker.sqlite3"), notifier=notifier, account_tail="…7777",
                            every_secs=60)
    for _ in range(3):
        report = reconciler.run(NOW, None)
    assert report["skipped_in_row"] == 3
    assert notifier.calls == [("alarm", [], [], "Счёт недоступен: три сверки подряд пропущены.")]


def test_lost_account_on_step_is_lost_not_maintenance(tmp_path):
    stop = Stop()
    lost = BrokerCallError("accounts.GetAccount", "UNAVAILABLE")
    session = ReadOnlySession(stop, reads=[None, lost, lost, lost], stop_after=2)   # и снимок при остановке
    contour, _ = _contour(tmp_path, session, stop)
    assert contour.run() == 0
    assert _status(tmp_path)["account_link"] == "lost"


def test_outage_is_a_broker_call_error():
    """Общий код, не знающий окна, видит обычную ошибку вызова."""
    assert issubclass(BrokerOutage, BrokerCallError)


def test_read_only_session_is_not_a_trading_session():
    assert not isinstance(ReadOnlySession(Stop()), TradingSession)
