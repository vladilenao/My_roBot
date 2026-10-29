"""Контур на брокере (add-sandbox-contour): допуск, отпечаток, сверщик, шаг процесса на фейковой песочнице."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from t_tech.invest import schemas as s

from src.assembly.core_state import CoreStateStore, Member
from src.broker_contour.account import AccountSnapshot
from src.broker_contour.admission import AdmissionError, admit_account, check_settings
from src.broker_contour.process import BrokerContour, BrokerContourSettings
from src.broker_contour.reconcile import JournalView, Reconciler, compare
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore, read_last_report
from src.events.bus import EventBus
from src.events.types import EventType
from src.sandbox.gateway import SandboxGateway
from tests.unit.sandbox.fakes import ACCOUNT_ID, NG_UID, TOKEN, FakeBroker, FakeClient, robot_account

TRADING_CALLS = {"orders.post_order", "orders.cancel_order", "stop_orders.post_stop_order",
                 "stop_orders.cancel_stop_order"}


class Collector:
    supported_types = frozenset({EventType.BROKER_ACCOUNT})

    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)

    def kinds(self):
        return [event.get("kind") for event in self.events]


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def alarm(self, items, frozen, *, reason=None):
        self.calls.append(("alarm", list(items), list(frozen), reason))

    def cleared(self, unfrozen):
        self.calls.append(("cleared", list(unfrozen)))


class Stop:
    def __init__(self):
        self.requested = threading.Event()
        self.reason = ""

    def request(self, reason):
        if not self.requested.is_set():
            self.reason = reason
            self.requested.set()


def _gateway(broker):
    return lambda token: SandboxGateway(token, client_factory=lambda value: FakeClient(broker, value),
                                        sleep=lambda seconds: None)


def _settings(tmp_path, **overrides):
    values = dict(contour_id="sandbox", broker="sandbox", directory=tmp_path / "contours" / "sandbox",
                  core_db=tmp_path / "core.sqlite3", version="9.9.9", token=TOKEN, token_env="TINKOFF_SANDBOX_TOKEN",
                  bot_token="123456:fake-bot-token-abcdef", bot_env="TELEGRAM_ACCOUNT_BOT_TOKEN",
                  channel_id="-100500", channel_env="TELEGRAM_SANDBOX_CHANNEL_ID", reconcile_every_secs=0,
                  step_secs=0.02)
    values.update(overrides)
    return BrokerContourSettings(**values)


def _launch(tmp_path, bindings=("future-ng-macd",)) -> CoreStateStore:
    store = CoreStateStore(tmp_path / "core.sqlite3")
    members = [Member(item, {"id": item, "instrument": "NG", "strategy": "macd_rsi_stoch", "timeframe": "15m"})
               for item in bindings]
    store.record_launch("sandbox", "sandbox", [], source="api", members=members)
    return store


def _status(tmp_path) -> dict:
    return json.loads((tmp_path / "contours" / "sandbox" / "status.json").read_text(encoding="utf-8"))


def _snapshot(holdings=(), orders=(), stops=()) -> AccountSnapshot:
    return AccountSnapshot(Decimal(1_000_000), None, tuple(holdings), tuple(orders), tuple(stops))


# --- допуск и отпечаток ------------------------------------------------------

@pytest.mark.parametrize("missing, text", [
    ("token", "Не задан TINKOFF_SANDBOX_TOKEN"),
    ("bot_token", "Не задан TELEGRAM_ACCOUNT_BOT_TOKEN"),
    ("channel_id", "Не задан TELEGRAM_SANDBOX_CHANNEL_ID"),
])
def test_settings_refused_without_network(missing, text):
    values = dict(token=TOKEN, token_env="TINKOFF_SANDBOX_TOKEN", bot_token="b", bot_env="TELEGRAM_ACCOUNT_BOT_TOKEN",
                  channel_id="c", channel_env="TELEGRAM_SANDBOX_CHANNEL_ID")
    values[missing] = None
    with pytest.raises(AdmissionError) as exc:
        check_settings(**values)
    assert exc.value.reason.startswith(text) and not exc.value.alarm


def test_first_start_opens_account_and_writes_fingerprint(tmp_path):
    broker = FakeBroker(rub=Decimal(0))
    store = BrokerStore(tmp_path / BROKER_DATABASE_FILE)
    with _gateway(broker)(TOKEN) as gateway:
        admitted = admit_account(gateway, store, broker="sandbox")
    assert admitted.fingerprint_created and admitted.account.opened
    fingerprint = store.fingerprint()
    assert fingerprint.account_tail == "…a1b2" and ACCOUNT_ID not in (tmp_path / BROKER_DATABASE_FILE).read_bytes().decode(
        "utf-8", "ignore")


def test_foreign_fingerprint_refused(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    store = BrokerStore(tmp_path / BROKER_DATABASE_FILE)
    from src.broker_contour.fingerprint import fingerprint_of
    store.save_fingerprint(fingerprint_of("sandbox", "sbx-other-account-09c3"))
    with _gateway(broker)(TOKEN) as gateway, pytest.raises(AdmissionError) as exc:
        admit_account(gateway, store, broker="sandbox")
    assert "другому счёту …09c3" in exc.value.reason and exc.value.alarm
    assert store.fingerprint().account_tail == "…09c3"


def test_two_robot_accounts_refused(tmp_path):
    broker = FakeBroker(accounts=[
        s.Account(id="sbx-first-0001", name="My_roBot", status=s.AccountStatus.ACCOUNT_STATUS_OPEN),
        s.Account(id="sbx-second-0002", name="My_roBot", status=s.AccountStatus.ACCOUNT_STATUS_OPEN)])
    with _gateway(broker)(TOKEN) as gateway, pytest.raises(AdmissionError) as exc:
        admit_account(gateway, BrokerStore(tmp_path / BROKER_DATABASE_FILE), broker="sandbox")
    assert "…0001" in exc.value.reason and "…0002" in exc.value.reason


# --- сверщик -------------------------------------------------------------------

def test_compare_counts_foreign_position_order_and_stop():
    items = compare(_snapshot([("NG-11.26", 1)], ["SBER"], ["NG-11.26"]), JournalView())
    assert items == [
        {"instrument": "NG-11.26", "what": "position", "account": 1, "journal": 0},
        {"instrument": "NG-11.26", "what": "stop_order", "account": 1, "journal": 0},
        {"instrument": "SBER", "what": "order", "account": 1, "journal": 0},
    ]


def test_reconciler_alarms_once_then_clears(tmp_path):
    notifier = FakeNotifier()
    store = BrokerStore(tmp_path / BROKER_DATABASE_FILE)
    reconciler = Reconciler(store=store, notifier=notifier, account_tail="…a1b2", every_secs=60)
    now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    assert reconciler.run(now, _snapshot())["result"] == "ok"
    report = reconciler.run(now, _snapshot([("NG-11.26", 1)]))
    assert report["result"] == "mismatch" and report["frozen"] == ["NG-11.26"]
    reconciler.run(now, _snapshot([("NG-11.26", 1)]))
    assert [call[0] for call in notifier.calls] == ["alarm"]
    reconciler.run(now, _snapshot())
    assert notifier.calls[-1] == ("cleared", ["NG-11.26"])
    assert read_last_report(tmp_path / BROKER_DATABASE_FILE)["result"] == "ok"


def test_reconciler_alarms_on_third_skip(tmp_path):
    notifier = FakeNotifier()
    reconciler = Reconciler(store=BrokerStore(tmp_path / BROKER_DATABASE_FILE), notifier=notifier,
                            account_tail="…a1b2", every_secs=60)
    now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    for _ in range(3):
        report = reconciler.run(now, None)
    assert report == {**report, "result": "skipped", "skipped_in_row": 3}
    assert notifier.calls == [("alarm", [], [], "Счёт недоступен: три сверки подряд пропущены.")]


def test_reconciler_schedule():
    reconciler = Reconciler(store=None, notifier=None, account_tail="…", every_secs=60)
    now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    assert reconciler.due(now)
    reconciler.last_at = now
    assert not reconciler.due(now + timedelta(seconds=59)) and reconciler.due(now + timedelta(seconds=60))


# --- процесс ----------------------------------------------------------------------

def _contour(tmp_path, broker, **overrides):
    bus, collector, stop = EventBus(), Collector(), Stop()
    bus.subscribe(collector)
    contour = BrokerContour(_settings(tmp_path, **overrides), gateway_factory=_gateway(broker), bus=bus, stop=stop,
                            pid=4242)
    return contour, collector, stop


def _run_in_thread(contour):
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", contour.run()), daemon=True)
    thread.start()
    return thread, result


def _wait(predicate, timeout=5):
    deadline = datetime.now() + timedelta(seconds=timeout)
    while datetime.now() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return False


def test_process_reads_account_reconciles_and_never_trades(tmp_path):
    broker = FakeBroker(rub=Decimal(1_000_000))
    robot_account(broker)
    broker.positions[NG_UID] = 1  # ручная покупка командой песочницы
    store = _launch(tmp_path)
    contour, collector, stop = _contour(tmp_path, broker)
    thread, result = _run_in_thread(contour)
    assert _wait(lambda: (tmp_path / "contours/sandbox/status.json").exists() and _status(tmp_path)["state"] == "running")
    status = _status(tmp_path)
    assert status["account"] == "…a1b2" and status["capital"]["rub"] == "1000000.00"
    assert status["frozen"] == ["NG-12.26"] and status["positions"] == [{"name": "NG-12.26", "lots": 1}]
    assert status["applied_generation"] == 1 and [m["key"] for m in status["members"]] == ["future-ng-macd"]
    assert collector.kinds()[:2] == ["started", "reconcile_alarm"]
    store.record_stop("sandbox", "close_all")
    thread.join(timeout=5)
    assert result["code"] == 0 and _status(tmp_path)["state"] == "stopped"
    assert broker.positions == {NG_UID: 1}, "close_all не трогает ручную позицию"
    assert not TRADING_CALLS & {name for name, _ in broker.calls}
    assert collector.kinds().count("reconcile_alarm") == 1 and collector.kinds()[-1] == "stopped"
    text = (tmp_path / "contours/sandbox/status.json").read_text(encoding="utf-8")
    assert ACCOUNT_ID not in text and TOKEN not in text


def test_process_applies_new_generation(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    store = _launch(tmp_path)
    contour, collector, stop = _contour(tmp_path, broker)
    thread, _ = _run_in_thread(contour)
    assert _wait(lambda: (tmp_path / "contours/sandbox/status.json").exists() and _status(tmp_path)["state"] == "running")
    store.record_bindings("sandbox", "sandbox", add=[Member("share-sber-ma", {"id": "share-sber-ma"})])
    assert _wait(lambda: _status(tmp_path)["applied_generation"] == 2)
    assert {m["key"] for m in _status(tmp_path)["members"]} == {"future-ng-macd", "share-sber-ma"}
    stop.request("тест")
    thread.join(timeout=5)
    assert "reconcile_alarm" not in collector.kinds()


def test_process_refuses_without_channel(tmp_path):
    broker = FakeBroker()
    _launch(tmp_path)
    contour, collector, _ = _contour(tmp_path, broker, channel_id=None)
    assert contour.run() == 2
    status = _status(tmp_path)
    assert status["state"] == "failed" and "TELEGRAM_SANDBOX_CHANNEL_ID" in status["reason"]
    assert broker.calls == [] and collector.events == []


def test_process_refuses_foreign_fingerprint_with_alarm(tmp_path):
    broker = FakeBroker()
    robot_account(broker)
    _launch(tmp_path)
    from src.broker_contour.fingerprint import fingerprint_of
    BrokerStore(tmp_path / "contours/sandbox" / BROKER_DATABASE_FILE).save_fingerprint(
        fingerprint_of("sandbox", "sbx-other-account-09c3"))
    contour, collector, _ = _contour(tmp_path, broker)
    assert contour.run() == 2
    assert "…09c3" in _status(tmp_path)["reason"]
    assert collector.kinds() == ["reconcile_alarm"] and "Контур не стартует" in collector.events[0].get("reason")


# --- торговля привязками (add-tinvest-orders) -----------------------------------

class FakeManager:
    def __init__(self):
        self.consumed, self.frozen, self.dispatched = [], None, 0

    def consume(self, event):
        self.consumed.append(event)

    def dispatch(self, now):
        self.dispatched += 1

    def contour_remainder(self, key=None, *, assignment_id=None):
        return (0, 0)

    def freeze_instruments(self, keys):
        self.frozen = set(keys)

    def hold_entries(self, held):
        pass

    def stop_contour(self, mode):
        pass

    def stop_assignment(self, assignment_id, mode):
        pass


class FakeTradingPart:
    """Торговая часть без бота: строит брокера через подстановку и крутит один опрос до остановки."""

    def __init__(self, trading, control, strategies, stop):
        import sqlite3
        from types import SimpleNamespace

        self.trading, self.control, self.stop = trading, control, stop
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.executescript("CREATE TABLE fills(execution_id TEXT); CREATE TABLE events(event_id TEXT);")
        self.storage = SimpleNamespace(connection=connection)
        self.manager = FakeManager()
        self.runtime = None

    def build(self):
        from types import SimpleNamespace

        broker = self.trading.broker_factory(self.storage)
        self.runtime = SimpleNamespace(trade_manager=self.manager, broker=broker)

    def waiter(self):
        return None

    def run(self, waiter):
        self.control.start(self.manager, None)
        while not self.stop.requested.wait(0.02):
            self.trading.poll()
            self.control.after_tick()

    def close(self):
        pass


def test_process_with_orders_recovers_reconciles_and_stops(tmp_path):
    broker = FakeBroker(rub=Decimal(1_000_000))
    robot_account(broker)
    store = _launch(tmp_path)
    collector, stop = Collector(), Stop()
    collector.supported_types = frozenset({EventType.BROKER_ACCOUNT, EventType.BROKER_ORDER})
    bus = EventBus()
    bus.subscribe(collector)
    parts = []

    def factory(trading, control, strategies):
        parts.append(FakeTradingPart(trading, control, strategies, stop))
        return parts[-1]

    contour = BrokerContour(_settings(tmp_path, orders=True), gateway_factory=_gateway(broker), bus=bus, stop=stop,
                            pid=4242, trading_factory=factory)
    thread, result = _run_in_thread(contour)
    assert _wait(lambda: (tmp_path / "contours/sandbox/status.json").exists()
                 and _status(tmp_path).get("stream_link") == "ok" and _status(tmp_path).get("members"))
    status = _status(tmp_path)
    assert status["orders_enabled"] is True and status["trades"] == 0 and status["orders_in_flight"] == 0
    assert status["members"][0]["state"] == "rejected", "привязка С2 без инструмента не торгует"
    assert parts[0].manager.frozen == set() and parts[0].manager.dispatched > 0
    started = next(event for event in collector.events if event.get("kind") == "started")
    assert started.get("orders_enabled") is True
    store.record_stop("sandbox", "no_entries")
    thread.join(timeout=5)
    assert result["code"] == 0 and _status(tmp_path)["state"] == "stopped"
    assert collector.kinds()[-1] == "stopped" and collector.events[-1].get("open_positions") == 0
    assert not TRADING_CALLS & {name for name, _ in broker.calls}
    assert "orders_stream.order_state_stream" in {name for name, _ in broker.calls}
    text = (tmp_path / "contours/sandbox/status.json").read_text(encoding="utf-8")
    assert ACCOUNT_ID not in text and TOKEN not in text
