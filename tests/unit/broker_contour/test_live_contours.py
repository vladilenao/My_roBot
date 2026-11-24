"""Описание контуров на брокере (refactor-broker-port, решение 6; tasks 3.3).

«Счёт» и песочница собираются по описаниям с прежними номерами, видами, отпечатками, папками и метками;
номер контура и вид — разные поля, поэтому второй экземпляр вида «счёт» добавляется описанием.
"""

from __future__ import annotations

import json

from src.assembly.broker_supervisor import BrokerContourSupervisor
from src.assembly.core_state import CoreStateStore, Member
from src.broker_contour.contours import ACCOUNT_CONTOUR, LIVE_CONTOURS, SANDBOX_CONTOUR, live_contour
from src.broker_contour.process import BrokerContour, BrokerContourSettings
from src.events.bus import EventBus
from tests.unit.assembly.test_broker_supervisor import FakeAdopted, FakeProcess
from tests.unit.broker_contour.test_broker_contour import Stop


def test_descriptions_keep_previous_values():
    assert [contour.contour_id for contour in LIVE_CONTOURS] == ["account", "sandbox"]   # порядок подъёма
    assert (ACCOUNT_CONTOUR.kind, ACCOUNT_CONTOUR.broker, ACCOUNT_CONTOUR.title) == ("account", "account", "СЧЁТ")
    assert (ACCOUNT_CONTOUR.token_env, ACCOUNT_CONTOUR.account_id_env) == ("TINKOFF_TRADE_TOKEN", "TINKOFF_ACCOUNT_ID")
    assert (SANDBOX_CONTOUR.kind, SANDBOX_CONTOUR.broker, SANDBOX_CONTOUR.title) == ("sandbox", "sandbox", "ПЕСОЧНИЦА")
    assert (SANDBOX_CONTOUR.token_env, SANDBOX_CONTOUR.account_id_env) == ("TINKOFF_SANDBOX_TOKEN", None)
    assert live_contour("emulation") is None and live_contour("account") is ACCOUNT_CONTOUR


def test_card_titles_come_from_descriptions():
    from src.broker_contour.notify import CONTOUR_TITLES as notify_titles
    from src.notifier.telegram_templates import CONTOUR_TITLES as template_titles

    assert notify_titles == template_titles == {"account": "СЧЁТ", "sandbox": "ПЕСОЧНИЦА"}


def test_supervisors_in_description_order_with_own_folders(tmp_path):
    import run

    supervisors = run._broker_supervisors(tmp_path, CoreStateStore(tmp_path / "core.sqlite3"))
    assert list(supervisors) == ["account", "sandbox"]
    assert {key: (s.contour_id, s.kind, s.directory) for key, s in supervisors.items()} == {
        "account": ("account", "account", tmp_path / "contours" / "account"),
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
