"""Команды состава контура: разбор, ожидание главного потока, обработка ядром (add-contour-membership)."""

import threading
from types import SimpleNamespace

import pytest

from src.assembly import core as core_module
from src.assembly.core_state import CoreStateStore
from src.bot.launch import Chosen, Rejection, Resolution, SOURCE_API
from src.bot.launch_control import BadCommand, LaunchControl, MembershipCommand, parse_membership
from src.portfolio.models import ContractMeta

SBER = ("SBER", "SBER", "share", "SBER")
NG = ("NG-12.26", "NGZ6", "future", "NG-12.26")


class TestParseMembership:
    def test_add_and_remove_with_default_mode(self):
        assert parse_membership({"add": ["NG", "NG", " SBER "]}).add == ("NG", "SBER")
        command = parse_membership({"remove": ["SBER"]})
        assert (command.remove, command.mode) == (("SBER",), "no_entries")
        assert parse_membership({"remove": ["NG"], "mode": "close_all"}).mode == "close_all"

    @pytest.mark.parametrize("body", [
        None, [], {}, {"add": []}, {"add": ["NG"], "remove": ["SBER"]}, {"add": ["NG"], "mode": "close_all"},
        {"remove": ["SBER"], "mode": "pause"}, {"add": [""]}, {"add": "NG"}, {"remove": ["SBER"], "extra": 1},
    ])
    def test_bad_bodies(self, body):
        with pytest.raises(BadCommand):
            parse_membership(body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Ядро в момент надзора: процесс контура жив, состав запуска — SBER."""
    store = CoreStateStore(tmp_path / "core.sqlite3")
    control = LaunchControl(reply_timeout=2, core_state=store, contour_dir=tmp_path / "contours" / "emulation")
    control.mark_launched(SOURCE_API, [Chosen("SBER", "SBER", "share")], "--mode emulation --instruments SBER")
    control.mark_process(4242)
    sber = core_module._normalized(SBER)
    store.record_launch("emulation", "emulation", [], members=[core_module._member(sber, {}, control.chosen_payloads())])
    monkeypatch.setattr(core_module, "_load_contracts_metadata", lambda instruments, **kw: {
        item.ticker: ContractMeta(item.ticker, 0.001, 8.2, 6500.0, 6500.0) for item in instruments})
    live = core_module.LiveCore.__new__(core_module.LiveCore)
    live.client_provider = live.request_budget = None
    pairs = []
    live._pattern_trigger = SimpleNamespace(add_pairs=pairs.extend)
    monkeypatch.setattr(core_module, "_pattern_pairs", lambda instruments: tuple((item, "1h") for item in instruments))
    added = []
    poller = SimpleNamespace(add_instruments=added.extend)
    resolutions = {"NG": Resolution([NG], [Chosen("NG", "NG-12.26", "future", 52, "NG", True)]),
                   "AFLT": Resolution([], [], [Rejection("AFLT", "У инструмента нет привязок в robot.toml.")])}

    def run(command):
        live._membership_command(command, kind="emulation", control=control, core_state=store, poller=poller,
                                 resolve=lambda tokens: resolutions[tokens[0]])
        return command.status, command.payload
    return SimpleNamespace(store=store, control=control, run=run, pairs=pairs, added=added)


def test_add_records_member_with_contract_and_extends_core(env):
    status, payload = env.run(MembershipCommand(add=("NG",)))
    assert status == 202 and payload["generation"] == 2
    member = env.store.desired("emulation").members[1]
    assert (member.key, member.chosen["token"], member.contract["NGZ6"]["price_step"]) == ("NGZ6", "NG", 0.001)
    assert [item.ticker for item in env.added] == ["NGZ6"] and env.pairs[0][1] == "1h"
    assert [item["name"] for item in payload["contour"]["instruments"]] == ["SBER", "NG-12.26"]
    assert payload["contour"]["members"][1] == {"name": "NG-12.26", "instrument_type": "future", "desired": "active",
                                                "remove_mode": None, "actual": None}


def test_rejected_add_changes_nothing(env):
    status, payload = env.run(MembershipCommand(add=("AFLT",)))
    assert status == 422 and payload["rejections"][0]["token"] == "AFLT"
    assert env.store.desired("emulation").generation == 1 and env.store.commands()[0]["outcome"] == "rejected"


def test_remove_repeat_and_conflict(env):
    assert env.run(MembershipCommand(remove=("SBER",)))[0] == 202
    assert env.run(MembershipCommand(remove=("sber",)))[0] == 200
    assert env.run(MembershipCommand(remove=("SBER",), mode="close_all"))[0] == 202
    status, payload = env.run(MembershipCommand(remove=("SBER",)))
    assert status == 409 and payload["code"] == "MODE_CONFLICT"


def test_remove_unknown_is_rejected(env):
    status, payload = env.run(MembershipCommand(remove=("GAZP",)))
    assert status == 422 and payload["rejections"] == [{"token": "GAZP", "reason": "Инструмента нет в составе контура."}]


class TestChangeMembership:
    def test_answer_from_main_thread(self, env):
        def main():
            command = env.control.next_membership(2)
            env.control.answer_membership(command, 202, {"generation": 7, "contour": {}})
        threading.Thread(target=main, daemon=True).start()
        assert env.control.change_membership("emulation", MembershipCommand(add=("NG",))) == (
            202, {"generation": 7, "contour": {}})

    def test_slow_main_thread_points_to_state(self, env):
        env.control._reply_timeout = 0.05
        status, payload = env.control.change_membership("emulation", MembershipCommand(add=("NG",)))
        assert status == 202 and payload["generation"] is None

    def test_conflicts(self, env):
        assert env.control.change_membership("account", MembershipCommand(add=("NG",)))[0] == 404
        env.store.record_stop("emulation", "no_entries")
        status, payload = env.control.change_membership("emulation", MembershipCommand(add=("NG",)))
        assert status == 409 and payload["code"] == "STOPPING"
        env.control.mark_exited(0)
        status, payload = env.control.change_membership("emulation", MembershipCommand(add=("NG",)))
        assert status == 409 and payload["code"] == "NOT_RUNNING"

    def test_supervise_dispatches_membership_commands(self, env):
        process = SimpleNamespace(running=True)
        handled = []

        def membership(command):
            handled.append(command.add)
            env.control.answer_membership(command, 202, {"generation": 2, "contour": {}})
            process.running = False

        live = core_module.LiveCore.__new__(core_module.LiveCore)
        live.market_store = SimpleNamespace(consumer_checkpoint=lambda consumer: None)
        threading.Thread(target=lambda: env.control.change_membership("emulation", MembershipCommand(add=("NG",))),
                         daemon=True).start()
        live._supervise(process, kind="emulation", control=env.control, poll_secs=0.05, membership=membership)
        assert handled == [("NG",)]
