"""Описание контуров на брокере (refactor-broker-port, решение 6; tasks 3.3).

«Счёт» и песочница собираются по описаниям с прежними номерами, видами, отпечатками, папками и метками;
номер контура и вид — разные поля, поэтому второй экземпляр вида «счёт» добавляется описанием.
"""

from __future__ import annotations

import json

from src.assembly.broker_supervisor import BrokerContourSupervisor
from src.assembly.core_state import CoreStateStore, Member
from src.broker_contour.contours import ACCOUNT_CONTOUR, FINAM_CONTOUR, LIVE_CONTOURS, SANDBOX_CONTOUR, live_contour
from src.broker_contour.process import BrokerContour, BrokerContourSettings
from src.events.bus import EventBus
from tests.unit.assembly.test_broker_supervisor import FakeAdopted, FakeProcess
from tests.unit.broker_contour.test_broker_contour import Stop


def test_descriptions_keep_previous_values():
    # Порядок подъёма: «Счёт», «Счёт Финам» (add-finam-contour), песочница.
    assert [contour.contour_id for contour in LIVE_CONTOURS] == ["account", "finam", "sandbox"]
    assert (ACCOUNT_CONTOUR.kind, ACCOUNT_CONTOUR.broker, ACCOUNT_CONTOUR.title) == ("account", "account", "СЧЁТ")
    assert (ACCOUNT_CONTOUR.token_env, ACCOUNT_CONTOUR.account_id_env) == ("TINKOFF_TRADE_TOKEN", "TINKOFF_ACCOUNT_ID")
    assert (SANDBOX_CONTOUR.kind, SANDBOX_CONTOUR.broker, SANDBOX_CONTOUR.title) == ("sandbox", "sandbox", "ПЕСОЧНИЦА")
    assert (SANDBOX_CONTOUR.token_env, SANDBOX_CONTOUR.account_id_env) == ("TINKOFF_SANDBOX_TOKEN", None)
    assert live_contour("emulation") is None and live_contour("account") is ACCOUNT_CONTOUR


def test_card_titles_come_from_descriptions():
    from src.broker_contour.notify import CONTOUR_TITLES as notify_titles
    from src.notifier.telegram_templates import CONTOUR_TITLES as template_titles

    assert notify_titles == template_titles == {"account": "СЧЁТ", "finam": "СЧЁТ ФИНАМ", "sandbox": "ПЕСОЧНИЦА"}


def test_supervisors_in_description_order_with_own_folders(tmp_path):
    import run

    supervisors = run._broker_supervisors(tmp_path, CoreStateStore(tmp_path / "core.sqlite3"))
    assert list(supervisors) == ["account", "finam", "sandbox"]
    assert {key: (s.contour_id, s.kind, s.directory) for key, s in supervisors.items()} == {
        "account": ("account", "account", tmp_path / "contours" / "account"),
        "finam": ("finam", "account", tmp_path / "contours" / "finam"),
        "sandbox": ("sandbox", "sandbox", tmp_path / "contours" / "sandbox")}


def test_launch_file_carries_contour_id_and_kind_apart(tmp_path):
    """Второй экземпляр вида «счёт»: своя папка, номер в ``launch.json`` — свой, вид — «счёт»."""
    store = CoreStateStore(tmp_path / "core.sqlite3")
    store.record_launch("second", "account", [], source="api", members=[Member("future-ng-macd", {"id": "x"})])
    supervisor = BrokerContourSupervisor(contour_id="second", kind="account", state_dir=tmp_path, core_state=store,
                                         version="9.9.9", autostart=True, heartbeat_timeout=30, heartbeat_secs=5,
                                         process_factory=FakeProcess, adopt_factory=FakeAdopted, say=lambda text: None)
    supervisor.launch(store.desired("second"))
    launch = json.loads((tmp_path / "contours/second/launch.json").read_text(encoding="utf-8"))
    assert (launch["contour_id"], launch["kind"]) == ("second", "account")
    assert FakeProcess.started[-1].kind == "second"          # процесс — run.py --contour <номер>


def test_status_kind_from_description_not_from_number(tmp_path):
    settings = BrokerContourSettings(
        contour_id="second", kind="account", broker="second", directory=tmp_path / "contours" / "second",
        core_db=tmp_path / "core.sqlite3", version="9.9.9", token=None, token_env="T", bot_token=None, bot_env="B",
        channel_id=None, channel_env="C", reconcile_every_secs=0)
    contour = BrokerContour(settings, session_factory=lambda: None, bus=EventBus(), stop=Stop(), pid=1)
    assert contour.run() == 2                                # без токена контур не стартует
    status = json.loads((tmp_path / "contours/second/status.json").read_text(encoding="utf-8"))
    assert (status["contour_id"], status["kind"]) == ("second", "account")


def test_finam_description():
    """«Счёт Финам» (add-finam-contour, решение 12): вид «счёт», свой отпечаток, брокер Финам, без заявок."""
    assert live_contour("finam") is FINAM_CONTOUR
    assert (FINAM_CONTOUR.kind, FINAM_CONTOUR.broker, FINAM_CONTOUR.provider, FINAM_CONTOUR.title) == (
        "account", "finam", "finam", "СЧЁТ ФИНАМ")
    assert (FINAM_CONTOUR.token_env, FINAM_CONTOUR.account_id_env) == ("FINAM_SECRET_TOKEN", "FINAM_ACCOUNT_ID")
    assert not FINAM_CONTOUR.trading and ACCOUNT_CONTOUR.trading and SANDBOX_CONTOUR.trading
    assert ACCOUNT_CONTOUR.provider == SANDBOX_CONTOUR.provider == "tinvest"


def test_console_label_from_descriptions():
    from src.notifier.templates.console_view import CONTOUR_TITLES as console_titles

    assert console_titles["finam"] == "СЧЁТ ФИНАМ"


def test_assembly_builds_finam_adapter_and_secrets(monkeypatch):
    """Точка сборки: адаптер Финама по описанию и секреты из описаний (add-finam-contour, решение 12)."""
    from types import SimpleNamespace

    import run

    settings = SimpleNamespace(token="fake-finam-secret", account_id="FAKE-ACC-1", orders=False)
    assert callable(run._broker_sessions(FINAM_CONTOUR, settings))
    monkeypatch.setenv("FINAM_SECRET_TOKEN", "fake-finam-secret-for-mask-0001")
    monkeypatch.setenv("FINAM_ACCOUNT_ID", "FAKE-FINAM-ACC-FOR-MASK-0002")
    monkeypatch.setenv("TELEGRAM_FINAM_BOT_TOKEN", "999:fake-finam-bot-for-mask-0003")
    secrets = run._live_contour_secrets()
    assert {"fake-finam-secret-for-mask-0001", "FAKE-FINAM-ACC-FOR-MASK-0002",
            "999:fake-finam-bot-for-mask-0003"} <= set(secrets)


def test_assembly_builds_live_resources(tmp_path):
    """Ресурсы API контуров вида «счёт» собираются по описаниям (add-finam-contour, решение 13)."""
    import run
    from src.account.fee_probes import fee_probe_extras
    from src.bot import control_views as views

    store = CoreStateStore(tmp_path / "core.sqlite3")
    live = run._live_supervisors(run._broker_supervisors(tmp_path, store))
    assert list(live) == [views.ACCOUNT, views.FINAM]
    resources = {base: run._account_control(spec, supervisor, store) for base, (spec, supervisor) in live.items()}
    account, finam = resources[views.ACCOUNT], resources[views.FINAM]
    assert (account.BASE, account.TITLE, account.provider) == ("/api/v1/account", "Счёт", "tinvest")
    assert (finam.BASE, finam.TITLE, finam.provider, finam.orders_enabled) == ("/api/v1/finam", "Счёт Финам", "finam",
                                                                               False)
    assert account._reconcile_extras is fee_probe_extras and finam._reconcile_extras is None
    assert ("POST", "/api/v1/account/guards/reset") in account.routes()
    assert ("POST", "/api/v1/finam/guards/reset") not in finam.routes()


def test_native_dns_only_for_finam_process():
    """Системный резолвер gRPC — только процессу контура, чьё описание его просит (add-finam-contour)."""
    from src.grpc_fork import dns_resolver_for

    assert dns_resolver_for(["--contour", "finam"]) == "native"
    assert dns_resolver_for(["--contour", "account"]) is None
    assert dns_resolver_for(["--contour", "sandbox"]) is None
    assert dns_resolver_for(["--contour", "emulation"]) is None
    assert dns_resolver_for([]) is None and dns_resolver_for(["--contour"]) is None


def test_grpc_fork_sets_resolver_before_grpc(tmp_path):
    """Процесс ``run.py --contour finam``: GRPC_DNS_RESOLVER задан до загрузки gRPC; внешнее значение не меняется."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    code = ("import sys; sys.argv = ['run.py', '--contour', 'finam']; import src.grpc_fork, os; "
            "print(os.environ.get('GRPC_DNS_RESOLVER'))")
    env = {key: value for key, value in os.environ.items() if key != "GRPC_DNS_RESOLVER"}
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "native"
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env={**env, "GRPC_DNS_RESOLVER": "ares"},
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ares"
