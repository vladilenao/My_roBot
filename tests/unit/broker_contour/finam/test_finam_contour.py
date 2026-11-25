"""Контур «Счёт Финам» целиком на фейковом Финаме (add-finam-contour; tasks 4.8, 6.1, 6.2).

Процесс контура на адаптере Финама: допуск, отпечаток, сверка, рестарт, секреты; и два живых контура
одновременно — «Счёт» на Т-Инвест и «Счёт Финам» — каждый со своим каталогом, базой и отпечатком.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from decimal import Decimal

from src.broker_contour.finam.session import adapter
from src.broker_contour.finam.transport import FinamTransport
from src.broker_contour.fingerprint import fingerprint_of
from src.broker_contour.process import BrokerContour, BrokerContourSettings
from src.broker_contour.store import BROKER_DATABASE_FILE, BrokerStore
from src.events.bus import EventBus
from tests.unit.broker_contour.finam.fakes import (ACCOUNT_ID, NG_CARD, SBER_CARD, SECRET, SI, SI_CARD, FakeCatalog,
                                                   FakeFinam, S, T, stubs)
from tests.unit.broker_contour.test_account_contour import ACCOUNT_ID as TINVEST_ACCOUNT_ID
from tests.unit.broker_contour.test_account_contour import _account, _sessions
from tests.unit.broker_contour.test_account_contour import _settings as tinvest_settings
from tests.unit.broker_contour.test_broker_contour import Collector, Stop
from tests.unit.sandbox.fakes import FakeBroker

BOT_TOKEN = "123456:fake-finam-bot-token-abcdef"
CHANNEL = "-100900"


def _settings(tmp_path) -> BrokerContourSettings:
    return BrokerContourSettings(
        contour_id="finam", kind="account", broker="finam", provider="finam",
        directory=tmp_path / "contours" / "finam", core_db=tmp_path / "core.sqlite3", version="9.9.9", token=SECRET,
        token_env="FINAM_SECRET_TOKEN", account_id=ACCOUNT_ID, account_id_env="FINAM_ACCOUNT_ID", bot_token=BOT_TOKEN,
        bot_env="TELEGRAM_FINAM_BOT_TOKEN", channel_id=CHANNEL, channel_env="TELEGRAM_FINAM_CHANNEL_ID",
        reconcile_every_secs=0, step_secs=0.02)


def _factory(fake):
    @contextmanager
    def catalog_factory():
        yield FakeCatalog(SI_CARD, NG_CARD, SBER_CARD)

    def transport_factory(secret, account_id):
        return FinamTransport(secret, account_id=account_id, channel_factory=lambda target: None,
                              stub_factory=stubs(fake), sleep=lambda seconds: None, renewal_backoff=(0.01,))

    return adapter(secret=SECRET, account_id=ACCOUNT_ID, catalog_factory=catalog_factory,
                   transport_factory=transport_factory)


def _run(tmp_path, fake, *, reads=3, during=None):
    collector, bus, stop = Collector(), EventBus(), Stop()
    bus.subscribe_all([collector])
    contour = BrokerContour(_settings(tmp_path), session_factory=_factory(fake), bus=bus, stop=stop, pid=4343)
    code = []
    thread = threading.Thread(target=lambda: code.append(contour.run()))
    thread.start()
    for _ in range(500):
        if fake.names().count("GetAccount") >= reads:
            break
        threading.Event().wait(0.01)
    if during is not None:
        during()
        target = fake.names().count("GetAccount") + 2
        for _ in range(500):
            if fake.names().count("GetAccount") >= target:
                break
            threading.Event().wait(0.01)
    stop.request("тест")
    thread.join(5)
    status = json.loads((tmp_path / "contours" / "finam" / "status.json").read_text(encoding="utf-8"))
    return collector, status, code


def test_read_only_contour_on_empty_account(tmp_path):
    fake = FakeFinam()
    collector, status, code = _run(tmp_path, fake)
    assert code == [0]
    started = next(event for event in collector.events if event.get("kind") == "started")
    assert (started.get("contour"), started.get("account"), started.get("access_level")) == ("finam", "…3322",
                                                                                            "read_only")
    assert (status["broker"], status["kind"], status["account"], status["account_link"]) == ("finam", "account",
                                                                                            "…3322", "ok")
    assert status["admission"]["access_level"] == "read_only" and status["capital"]["rub"] == "0.00"
    assert "reconcile_alarm" not in collector.kinds()
    assert not {"PlaceOrder", "CancelOrder", "PlaceSLTPOrder"} & set(fake.names())
    assert BrokerStore(tmp_path / "contours" / "finam" / BROKER_DATABASE_FILE).fingerprint().broker == "finam"


def test_manual_order_is_a_mismatch_and_cleared_after_cancel(tmp_path):
    fake = FakeFinam(cash=[("30000", "RUB")])

    def manual_order():
        fake.orders.append((SI, T.ORDER_TYPE_LIMIT, S.ORDER_STATUS_NEW))

    collector, status, _ = _run(tmp_path, fake, during=manual_order)
    alarm = next(event for event in collector.events if event.get("kind") == "reconcile_alarm")
    assert alarm.get("contour") == "finam" and "Si-12.26" in json.dumps(dict(alarm.payload), ensure_ascii=False, default=str)
    assert status["frozen"] == ["Si-12.26"] and status["orders"] == 1

    fake.orders.clear()
    collector, status, _ = _run(tmp_path, fake)
    assert status["frozen"] == []


def test_restart_keeps_fingerprint_without_first_start_alarm(tmp_path):
    fake = FakeFinam(cash=[("30000", "RUB")], positions=[(SI, "1")])
    collector, _, _ = _run(tmp_path, fake)
    first = [event.get("reason") for event in collector.events if event.get("kind") == "reconcile_alarm"]
    assert first == ["Счёт не пуст на первом старте: Si-12.26"]
    collector, status, code = _run(tmp_path, fake)
    assert code == [0] and status["state"] == "stopped"
    assert all(event.get("reason") != first[0] for event in collector.events)


def test_foreign_fingerprint_refused_with_alarm(tmp_path):
    store = BrokerStore(tmp_path / "contours" / "finam" / BROKER_DATABASE_FILE)
    store.save_fingerprint(fingerprint_of("finam", "FAKE-FINAM-OTHER-ACC-0909"))
    collector, status, code = _run(tmp_path, FakeFinam())
    assert code == [2] and status["state"] == "failed" and "…0909" in status["reason"]
    assert "refused" in collector.kinds()


def test_two_accounts_refused_with_alarm(tmp_path):
    collector, status, code = _run(tmp_path, FakeFinam(account_ids=[ACCOUNT_ID, "FAKE-FINAM-CONTOUR-ACC-11112222"]))
    assert code == [2] and "счетов: 2" in status["reason"] and "refused" in collector.kinds()


def test_secrets_nowhere(tmp_path, caplog):
    """FINAM_SECRET_TOKEN, номер счёта и каждый JWT — нет в логе, status.json, базе и событиях канала."""
    caplog.set_level(logging.DEBUG)
    fake = FakeFinam(cash=[("30000", "RUB")])
    collector, _, _ = _run(tmp_path, fake, reads=5)
    secrets = [SECRET, ACCOUNT_ID, *fake.issued]
    assert len(fake.issued) >= 2                       # JWT входа и JWT стрима обновления
    texts = [caplog.text, json.dumps([dict(event.payload) for event in collector.events], ensure_ascii=False, default=str)]
    for path in (tmp_path / "contours" / "finam").iterdir():
        texts.append(path.read_bytes().decode("utf-8", "ignore"))
    for text in texts:
        for secret in secrets:
            assert secret not in text


def test_account_and_finam_side_by_side(tmp_path):
    """Два живых контура вида «счёт» одновременно: свои каталоги, базы, отпечатки и статусы."""
    tinvest = FakeBroker(accounts=[_account()], rub=Decimal("150000"))
    finam = FakeFinam(cash=[("30000", "RUB")])
    stops = {"account": Stop(), "finam": Stop()}
    contours = {
        "account": BrokerContour(tinvest_settings(tmp_path), session_factory=_sessions(tinvest, orders=False),
                                 bus=EventBus(), stop=stops["account"], pid=1),
        "finam": BrokerContour(_settings(tmp_path), session_factory=_factory(finam), bus=EventBus(),
                               stop=stops["finam"], pid=2),
    }
    threads = [threading.Thread(target=contour.run) for contour in contours.values()]
    for thread in threads:
        thread.start()
    for _ in range(500):
        if finam.names().count("GetAccount") >= 3 and \
                [m for m, _ in tinvest.calls].count("operations.get_positions") >= 3:
            break
        threading.Event().wait(0.01)
    for stop in stops.values():
        stop.request("тест")
    for thread in threads:
        thread.join(5)
    statuses = {name: json.loads((tmp_path / "contours" / name / "status.json").read_text(encoding="utf-8"))
                for name in contours}
    assert {name: (status["broker"], status["account"], status["capital"]["rub"])
            for name, status in statuses.items()} == {"account": (None, "…7f21", "150000.00"),
                                                      "finam": ("finam", "…3322", "30000.00")}
    prints = {name: BrokerStore(tmp_path / "contours" / name / BROKER_DATABASE_FILE).fingerprint()
              for name in contours}
    assert (prints["account"].broker, prints["account"].account_tail) == ("account", "…7f21")
    assert (prints["finam"].broker, prints["finam"].account_tail) == ("finam", "…3322")
    assert TINVEST_ACCOUNT_ID not in (tmp_path / "contours" / "finam" / BROKER_DATABASE_FILE).read_bytes().decode(
        "utf-8", "ignore")
