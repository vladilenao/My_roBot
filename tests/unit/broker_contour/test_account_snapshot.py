"""Снимок счёта перепроверяется по ``GetPositions`` (fix-account-snapshot-cross-check).

20.11.2026, «Счёт» …7029: после сбоя брокера (INTERNAL 70002) ``GetPortfolio`` отдавал пустой список,
а ``GetPositions`` — шорт Si −3. Сверка приняла пустой портфель, заморозила Si, хранитель стопа
срезал стоп до нуля и снял настоящую стоп-заявку. Позиция на реальном счёте осталась без стопа.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from t_tech.invest import schemas as s

from src.broker_contour.tinvest.account import INCONSISTENT, position_conflicts, read_account
from src.broker_contour.store import read_last_report
from src.events.bus import EventBus
from src.sandbox.gateway import BrokerCallError
from tests.unit.broker_contour.test_broker_contour import (
    Collector,
    FakeTradingPart,
    Stop,
    _gateway,
    _launch,
    _run_in_thread,
    _settings,
    _status,
    _wait,
)
from tests.unit.sandbox.fakes import ACCOUNT_ID, NG_UID, SBER_UID, TOKEN, FakeBroker, quote, robot_account

SI_UID = "uid-si-12-26"


def _portfolio(*items):
    return s.PortfolioResponse(positions=[
        s.PortfolioPosition(instrument_type="currency", quantity_lots=quote(48_345), instrument_uid="rub"),
        *(s.PortfolioPosition(instrument_type=kind, instrument_uid=uid, quantity_lots=quote(lots))
          for uid, kind, lots in items)])


def _positions(futures=(), securities=()):
    return s.PositionsResponse(
        futures=[s.PositionsFutures(instrument_uid=uid, balance=balance, blocked=blocked)
                 for uid, balance, blocked in futures],
        securities=[s.PositionsSecurities(instrument_uid=uid, balance=balance, blocked=blocked)
                    for uid, balance, blocked in securities])


# --- сравнение ответов -------------------------------------------------------------

def test_empty_portfolio_with_live_future_is_conflict():
    """Ровно 20.11: портфель пуст, позиции — шорт Si на 3 контракта."""
    assert position_conflicts(_portfolio(), _positions(futures=[(SI_UID, -3, 0)])) == [(SI_UID, 0, -3)]


@pytest.mark.parametrize("portfolio, positions", [
    (_portfolio((SI_UID, "futures", -3)), _positions(futures=[(SI_UID, -3, 0)])),
    (_portfolio(), _positions()),
    (_portfolio(), _positions(futures=[(SI_UID, 0, 0)])),                      # закрытая позиция строкой с нулём
    (_portfolio((SI_UID, "futures", 2)), _positions(futures=[(SI_UID, 1, 1)])),  # часть контрактов заблокирована
    (_portfolio((SBER_UID, "share", 3)), _positions(securities=[(SBER_UID, 30, 0)])),  # бумаги: лоты против штук
])
def test_consistent_snapshots(portfolio, positions):
    assert position_conflicts(portfolio, positions) == []


@pytest.mark.parametrize("portfolio, positions, conflict", [
    (_portfolio((SI_UID, "futures", -3)), _positions(), (SI_UID, -3, 0)),
    (_portfolio((SI_UID, "futures", -3)), _positions(futures=[(SI_UID, -2, 0)]), (SI_UID, -3, -2)),
    (_portfolio((SBER_UID, "share", 3)), _positions(), (SBER_UID, 3, 0)),
    (_portfolio(), _positions(securities=[(SBER_UID, 30, 0)]), (SBER_UID, 0, 30)),
    (_portfolio((SBER_UID, "share", 3)), _positions(securities=[(SBER_UID, -30, 0)]), (SBER_UID, 3, -30)),
])
def test_conflicting_snapshots(portfolio, positions, conflict):
    assert position_conflicts(portfolio, positions) == [conflict]


# --- чтение счёта ------------------------------------------------------------------

def test_read_account_refuses_inconsistent_snapshot(caplog):
    broker = FakeBroker(rub=Decimal(1_000_000), positions={NG_UID: -3}, portfolio_glitch={})
    robot_account(broker)
    from src.sandbox.account import RobotAccount
    account = RobotAccount(ACCOUNT_ID, opened=False)
    with caplog.at_level(logging.WARNING), _gateway(broker)(TOKEN) as gateway, \
            pytest.raises(BrokerCallError) as exc:
        read_account(gateway, account)
    assert exc.value.code == INCONSISTENT
    assert "Снимок счёта противоречив" in caplog.text and "NG-12.26 — портфель 0, позиции -3" in caplog.text
    assert ACCOUNT_ID not in caplog.text and TOKEN not in caplog.text


def test_read_account_accepts_consistent_snapshot():
    broker = FakeBroker(rub=Decimal(1_000_000), positions={NG_UID: -3})
    robot_account(broker)
    from src.sandbox.account import RobotAccount
    with _gateway(broker)(TOKEN) as gateway:
        snapshot = read_account(gateway, RobotAccount(ACCOUNT_ID, opened=False))
    assert snapshot.holdings == (("NG-12.26", -3),) and snapshot.rub == Decimal(1_000_000)


# --- процесс контура: сбойный портфель не доходит до сверки и хранителя стопа ----------

def test_glitched_portfolio_skips_reconcile_and_keeps_stop_guard_snapshot(tmp_path):
    broker = FakeBroker(rub=Decimal(1_000_000))
    robot_account(broker)
    _launch(tmp_path)
    stop = Stop()
    collector = Collector()
    bus = EventBus()
    bus.subscribe(collector)
    parts = []

    def factory(trading, control, strategies):
        parts.append(FakeTradingPart(trading, control, strategies, stop))
        return parts[-1]

    from src.broker_contour.process import BrokerContour
    contour = BrokerContour(_settings(tmp_path, orders=True), gateway_factory=_gateway(broker), bus=bus, stop=stop,
                            pid=4242, trading_factory=factory)
    thread, _ = _run_in_thread(contour)
    assert _wait(lambda: (tmp_path / "contours/sandbox/status.json").exists()
                 and _status(tmp_path).get("account_link") == "ok" and parts and parts[0].runtime is not None)
    seen = []
    real = parts[0].runtime.broker.set_account_positions
    parts[0].runtime.broker.set_account_positions = lambda holdings, at=None: (seen.append(holdings),
                                                                             real(holdings, at=at))
    report_path = tmp_path / "contours/sandbox/broker.sqlite3"

    broker.portfolio_glitch = {}            # портфель брокера пуст…
    broker.positions[NG_UID] = -3          # …а позиция на счёте есть
    assert _wait(lambda: (read_last_report(report_path) or {}).get("result") == "skipped")
    first_skip = read_last_report(report_path)["at"]
    seen.clear()                            # сверка, начатая до сбоя, могла успеть передать снимок
    assert _wait(lambda: read_last_report(report_path)["at"] > first_skip)
    last = read_last_report(report_path)
    assert last["result"] == "skipped" and last["items"] == [] and last["skipped_in_row"] >= 2
    assert _status(tmp_path)["account_link"] == "lost"
    assert parts[0].manager.frozen == set(), "противоречивый снимок не замораживает инструмент"
    assert seen == [], "пока снимок противоречив, хранитель стопа держит прежний"

    broker.portfolio_glitch = None          # брокер снова отвечает согласованно
    assert _wait(lambda: (read_last_report(report_path) or {}).get("result") == "mismatch")
    assert read_last_report(report_path)["items"][0]["account"] == -3
    assert _wait(lambda: bool(seen)) and seen[-1] == {NG_UID: -3}
    assert _wait(lambda: bool(parts[0].manager.frozen))
    stop.request("тест")
    thread.join(timeout=5)
    assert collector.kinds().count("reconcile_alarm") == 1, "тревога — по согласованному снимку, не по сбою"
