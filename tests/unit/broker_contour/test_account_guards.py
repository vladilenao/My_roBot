"""Предохранители контура на брокере (add-account-guards): дневной убыток, отказы подряд, запрет входов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker_contour.guards import (BROKER_REJECTS, DAILY_LOSS, GuardStore, Guards, day_result,
                                       trading_day_start)
from src.broker_contour.orders import OrderLedger
from src.trade_management.manager import GUARD_BROKER_REJECTS, GUARD_DAILY_LOSS
from tests.unit.bot.test_contour_trades_api import _Journal

D = Decimal
UTC = timezone.utc


def at(hour, minute=0, day=31):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Manager:
    def __init__(self):
        self.guards = {}

    def guard_entries(self, reason, active):
        self.guards[reason] = active


class Harness:
    def __init__(self, tmp_path, *, pct=1.0, rejects=3, now=None):
        self.clock = Clock(now or at(17, 10))
        self.ledger = OrderLedger(tmp_path / "broker.sqlite3")
        self.events = []
        self.guards = Guards(GuardStore(self.ledger), max_daily_loss_pct=pct, max_rejects_in_row=rejects,
                             clock=self.clock, tripped=lambda s: self.events.append(("tripped", s.kind, s.detail)),
                             cleared=lambda s, why: self.events.append(("cleared", s.kind, why)))
        self.manager = Manager()
        self.guards.attach(self.manager)


def test_trading_day_starts_at_19_msk():
    assert trading_day_start(at(17, 10)) == at(16)            # 20:10 МСК → день с 19:00 МСК
    assert trading_day_start(at(15, 59)) == at(16, day=30)     # 18:59 МСК → вчерашний день


def test_day_result_closed_partial_open_and_yesterday(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    # Вчера (до 19:00 МСК 31.10): закрыта с убытком — не входит.
    journal.open("old", "future-ng-macd", "NGX6", "BUY", 1, "100", at(10), step="1", cost="10")
    journal.close("old", 1, "90", at(15), "signal")
    # Сегодня: лонг 3 по 100, частичный выход 1 по 95, остаток 2 открыт, последняя цена 97.
    journal.open("today", "future-si-levels", "SIZ6", "BUY", 3, "100", at(16, 30), step="1", cost="10")
    journal.reduce("today", 1, "95", at(16, 50), "partial")
    result = day_result(journal.storage.connection, at(16), {"SIZ6": D("97")})
    # закрытый: (95−100)×1×10 − комиссии дня (вход 1 + выход 1) = −52; открытый: (97−100)×2×10 = −60
    assert (result.closed, result.open, result.loss) == (D("-52"), D("-60"), D("112"))


def test_day_result_short_and_missing_price(tmp_path):
    journal = _Journal(tmp_path / "trades.sqlite3")
    journal.open("short", "future-si-levels", "SIZ6", "SELL", 2, "100", at(16, 30), step="1", cost="10")
    journal.open("ng", "future-ng-macd", "NGX6", "BUY", 1, "50", at(16, 40), step="1", cost="10")
    result = day_result(journal.storage.connection, at(16), {"SIZ6": D("98")})
    assert result.open == D("40") and result.missing_prices == ("NGX6",)   # шорт в плюсе, у NG цены нет


def test_daily_loss_trips_until_next_day_and_clears(tmp_path):
    h = Harness(tmp_path, pct=1.0)
    journal = _Journal(tmp_path / "trades.sqlite3")
    journal.open("t1", "future-si-levels", "SIZ6", "BUY", 10, "100", at(16, 30), step="1", cost="100")
    h.clock.now = at(16) + timedelta(seconds=30)
    h.guards.evaluate_daily(D("100000"), journal.storage.connection, {"SIZ6": D("100")}, force=True)
    assert not h.manager.guards[GUARD_DAILY_LOSS]
    h.clock.now = at(17, 10)
    h.guards.evaluate_daily(D("90000"), journal.storage.connection, {"SIZ6": D("99")}, force=True)   # −1000 − 1
    assert h.manager.guards[GUARD_DAILY_LOSS]
    [(kind, guard, detail)] = h.events
    assert (kind, guard, detail["limit"], detail["start_capital"], detail["partial_day"]) == \
        ("tripped", DAILY_LOSS, "1000.00", "100000.00", False)
    # Цена вернулась — запрет остаётся до нового дня.
    h.guards.evaluate_daily(D("90000"), journal.storage.connection, {"SIZ6": D("110")}, force=True)
    assert h.manager.guards[GUARD_DAILY_LOSS] and len(h.events) == 1
    h.clock.now = datetime(2026, 11, 1, 16, 1, tzinfo=UTC)
    h.guards.evaluate_daily(D("95000"), journal.storage.connection, {"SIZ6": D("110")}, force=True)
    assert not h.manager.guards[GUARD_DAILY_LOSS] and h.events[-1] == ("cleared", DAILY_LOSS, "new-day")


def test_daily_loss_disabled_by_zero(tmp_path):
    h = Harness(tmp_path, pct=0)
    journal = _Journal(tmp_path / "trades.sqlite3")
    journal.open("t1", "future-si-levels", "SIZ6", "BUY", 10, "100", at(16, 30), step="1", cost="100")
    h.guards.evaluate_daily(D("100000"), journal.storage.connection, {"SIZ6": D("50")}, force=True)
    assert not h.manager.guards[GUARD_DAILY_LOSS] and h.events == []


def test_daily_loss_survives_restart_with_day_capital(tmp_path):
    h = Harness(tmp_path, pct=1.0)
    journal = _Journal(tmp_path / "trades.sqlite3")
    journal.open("t1", "future-si-levels", "SIZ6", "BUY", 10, "100", at(16, 30), step="1", cost="100")
    h.guards.evaluate_daily(D("100000"), journal.storage.connection, {"SIZ6": D("98")}, force=True)
    assert h.manager.guards[GUARD_DAILY_LOSS]
    h.ledger.close()
    h2 = Harness(tmp_path, pct=1.0, now=at(17, 20))
    assert h2.manager.guards[GUARD_DAILY_LOSS]                     # запрет применён до первого тика
    assert h2.guards.states()[0]["kind"] == DAILY_LOSS
    assert h2.guards._store.day(at(16)) == (D("100000"), True)      # контур поднялся посреди дня


def test_rejects_in_row_trip_and_reset(tmp_path):
    h = Harness(tmp_path, rejects=3)
    h.guards.order_outcome(False, "broker:30042")
    h.guards.order_outcome(False, "broker:30042")
    h.guards.order_outcome(True)
    assert h.guards._store.get(BROKER_REJECTS).detail == {}
    for code in ("broker:30034", "rate-limited", "broker:30042"):
        h.guards.order_outcome(False, code)
    assert h.manager.guards[GUARD_BROKER_REJECTS]
    assert h.events[-1][:2] == ("tripped", BROKER_REJECTS)
    assert h.events[-1][2] == {"count": 3, "reasons": ["broker:30034", "rate-limited", "broker:30042"]}
    h.guards.order_outcome(True)                                     # принятая заявка запрет не снимает
    assert h.manager.guards[GUARD_BROKER_REJECTS]
    h.guards.apply_reset(1)
    assert not h.manager.guards[GUARD_BROKER_REJECTS] and h.events[-1] == ("cleared", BROKER_REJECTS, "command")
    h.guards.apply_reset(1)                                          # то же поколение — ничего
    assert len(h.events) == 2


def test_rejects_counter_survives_restart(tmp_path):
    h = Harness(tmp_path, rejects=3)
    h.guards.order_outcome(False, "broker:30042")
    h.guards.order_outcome(False, "broker:30042")
    h.ledger.close()
    h2 = Harness(tmp_path, rejects=3)
    h2.guards.order_outcome(False, "broker:30042")
    assert h2.manager.guards[GUARD_BROKER_REJECTS]


def test_broker_counts_only_broker_rejects(tmp_path):
    """Отказ брокера на вход и стоп-заявку — в счётчик; проверки до отправки и «торги закрыты» — нет."""
    from src.broker_contour.port import OrderFailure
    from src.broker_contour.broker import ExitPolicy
    from src.broker_contour.orders import order_key
    from src.broker_contour.stops import StopLedger
    from src.trade_management.actions import EntryOrderType, OpenTrade
    from tests.unit.broker_contour.fake_api import NG, NG_META, Clock as ApiClock, FakeApi, live_broker
    from tests.unit.broker_contour.test_tinvest_broker import _plan

    outcomes = []
    api, ledger = FakeApi(), OrderLedger(tmp_path / "b.sqlite3")
    broker = live_broker(api, ledger, contour="sandbox", policy=ExitPolicy(5, 15, 2, 60), clock=ApiClock(),
                           stops=StopLedger(ledger), order_outcome=lambda ok, reason: outcomes.append((ok, reason)))
    broker.set_contracts({NG: NG_META})
    broker.register_trade(_plan())
    api.band = (D("2.9"), D("3.5"))
    out_of_band = OpenTrade("t:e0", "trade-ng-1", 0, "signal", 2, EntryOrderType.LIMIT, D("2.8457"))
    assert broker.submit(out_of_band, api_now := ApiClock()()).reason == "price-out-of-band"
    assert outcomes == []
    api.band = (D("2.5"), D("3.5"))
    api.failures.append(OrderFailure("rejected", "FAILED_PRECONDITION", "30042", "не хватает ГО"))
    rejected = OpenTrade("t:e1", "trade-ng-1", 0, "signal", 2, EntryOrderType.LIMIT, D("2.8457"))
    broker.submit(rejected, api_now)
    assert outcomes == [(False, "broker:30042")]
    accepted = OpenTrade("t:e2", "trade-ng-1", 0, "signal", 2, EntryOrderType.LIMIT, D("2.8457"))
    broker.submit(accepted, api_now)
    assert outcomes[-1] == (True, "")
    broker.ingest(api.fill(order_key(accepted.command_id), 2, "2.846"))
    broker.drain_addressed_events()
    api.stop_failures.append(OrderFailure("market_closed", "FAILED_PRECONDITION", "30079"))
    broker.service(api_now)
    assert outcomes[-1] == (True, "")                              # «торги закрыты» не считается


def test_manager_guard_reason_blocks_entries_and_two_reasons():
    from src.trade_management.manager import TradeManager

    manager = TradeManager.__new__(TradeManager)
    manager._contour_stop, manager._instrument_stops, manager._assignment_stops = None, {}, {}
    manager._frozen, manager._core_lost, manager._guards = frozenset(), False, set()

    class Traces:
        def record(self, trace):
            return None

    manager._traces = Traces()
    manager.guard_entries(GUARD_DAILY_LOSS, True)
    manager.guard_entries(GUARD_BROKER_REJECTS, True)
    assert manager._entry_block("SIZ6") == GUARD_DAILY_LOSS
    manager.guard_entries(GUARD_DAILY_LOSS, False)
    assert manager._entry_block("SIZ6") == GUARD_BROKER_REJECTS
    manager.guard_entries(GUARD_BROKER_REJECTS, False)
    assert manager._entry_block("SIZ6") is None
    with pytest.raises(ValueError):
        manager.guard_entries("other", True)


def test_guard_messages_render():
    from src.events.event import Event
    from src.notifier.telegram_templates import render_broker_account

    def text(kind, **fields):
        return render_broker_account(Event.broker_account(kind, contour="sandbox", account="…09c3", event_id="e",
                                                          **fields))

    tripped = text("guard_tripped", guard="daily_loss", since="2026-10-31T17:10:00+00:00",
                   until="2026-11-01T16:00:00+00:00",
                   guard_detail={"loss": "1001.00", "limit": "1000.00", "start_capital": "100000.00",
                                 "partial_day": False, "missing_prices": []})
    assert "ТРЕВОГА · Предохранитель: дневной убыток" in tripped and "1 001 ₽" in tripped
    assert "до 19:00 МСК" in tripped
    rejects = text("guard_tripped", guard="broker_rejects", guard_detail={"count": 3, "reasons": ["broker:30042"]})
    assert "Отказов брокера подряд: 3" in rejects and "POST /api/v1/sandbox/guards/reset" in rejects
    account = render_broker_account(Event.broker_account(
        "guard_tripped", contour="account", account="…7029", event_id="e", guard="broker_rejects",
        guard_detail={"count": 3, "reasons": ["broker:30042"]}))
    assert "POST /api/v1/account/guards/reset" in account and "/sandbox/" not in account
    assert "командой сброса" in text("guard_cleared", guard="broker_rejects", reason="command")
    assert "новый торговый день" in text("guard_cleared", guard="daily_loss", reason="new-day")


def test_unknown_day_capital_waits_for_capital(tmp_path):
    h = Harness(tmp_path, pct=1.0)
    h.guards.evaluate_daily(None, None, {}, force=True)
    assert h.guards._store.day(at(16)) is None
    h.clock.now += timedelta(seconds=11)
    h.guards.evaluate_daily(D("1000"), None, {})
    assert h.guards._store.day(at(16)) is not None
