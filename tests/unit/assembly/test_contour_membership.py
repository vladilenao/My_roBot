"""Команды состава эмуляции: разбор, ожидание главного потока, обработка ядром (add-contour-membership, rework-contour-api)."""

import threading
from types import SimpleNamespace

import pytest

from src.assembly import core as core_module
from src.assembly.core_state import CoreStateStore
from src.bot.launch import Chosen, Rejection, Resolution, SOURCE_API
from src.bot.launch_control import BadCommand, LaunchControl, MembershipCommand, parse_add, parse_remove
from src.portfolio.models import ContractMeta

SBER = ("SBER", "SBER", "share", "SBER")
NG = ("NG-12.26", "NGZ6", "future", "NG-12.26")


class TestParseMembership:
    def test_add_and_remove_with_default_mode(self):
        assert parse_add({"instruments": ["NG-12.26", "NG-12.26", " SBER "]}).add == ("NG-12.26", "SBER")
        command = parse_remove({"instruments": ["SBER", "NG-12.26"]}, expected=3)
        assert (command.remove, command.mode, command.expected) == (("SBER", "NG-12.26"), "no_entries", 3)
        assert parse_remove({"instruments": ["NG-12.26"], "mode": "close_all"}).mode == "close_all"

    @pytest.mark.parametrize("parse, body", [
        (parse_add, None), (parse_add, []), (parse_add, {}), (parse_add, {"instruments": []}),
        (parse_add, {"instruments": ["NG"], "mode": "close_all"}), (parse_add, {"instruments": [""]}),
        (parse_add, {"instruments": "NG"}), (parse_add, {"add": ["NG"]}),
        (parse_remove, {"instruments": ["SBER"], "mode": "pause"}), (parse_remove, {"instruments": ["SBER"], "extra": 1}),
        (parse_remove, {"mode": "close_all"}), (parse_remove, {"remove": ["SBER"]}),
    ])
    def test_bad_bodies(self, parse, body):
        with pytest.raises(BadCommand):
            parse(body)


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
    resolutions = {"NG-12.26": Resolution([NG], [Chosen("NG-12.26", "NG-12.26", "future", 52, "NG")]),
                   "AFLT": Resolution([], [], [Rejection("AFLT", "У инструмента нет привязок в robot.toml.")])}

    def run(command):
        live._membership_command(command, kind="emulation", control=control, core_state=store, poller=poller,
                                 resolve=lambda tokens: resolutions[tokens[0]])
        return command.status, command.payload
    return SimpleNamespace(store=store, control=control, run=run, pairs=pairs, added=added)


def test_add_records_member_with_contract_and_extends_core(env):
    status, payload = env.run(MembershipCommand(add=("NG-12.26",)))
    assert status == 202 and payload == {"revision": 2}
    member = env.store.desired("emulation").members[1]
    assert (member.key, member.chosen["token"], member.contract["NGZ6"]["price_step"]) == ("NGZ6", "NG-12.26", 0.001)
    assert [item.ticker for item in env.added] == ["NGZ6"] and env.pairs[0][1] == "1h"
    items = env.control.emulation()["instruments"]
    assert [item["name"] for item in items] == ["SBER", "NG-12.26"]
    assert {key: items[1][key] for key in ("instrument_type", "days_to_expiry", "target", "remove_mode", "current")} \
        == {"instrument_type": "future", "days_to_expiry": 52, "target": "active", "remove_mode": None, "current": None}


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


def test_remove_unknown_is_rejected_and_nothing_is_removed(env):
    status, payload = env.run(MembershipCommand(remove=("SBER", "GAZP")))
    assert status == 422 and payload["rejections"] == [{"token": "GAZP", "reason": "Инструмента нет в составе эмуляции."}]
    assert env.store.desired("emulation").members[0].active


def test_remove_by_family_code_is_not_matched(env):
    env.run(MembershipCommand(add=("NG-12.26",)))
    status, payload = env.run(MembershipCommand(remove=("NG",)))
    assert status == 422 and payload["rejections"][0]["token"] == "NG"


def test_remove_two_at_once_raises_revision_once(env):
    env.run(MembershipCommand(add=("NG-12.26",)))
    status, payload = env.run(MembershipCommand(remove=("NG-12.26", "SBER"), mode="close_all"))
    desired = env.store.desired("emulation")
    assert (status, payload["revision"], desired.generation) == (202, 3, 3)
    assert [(member.active, member.remove_mode) for member in desired.members] == [(False, "close_all")] * 2


def test_stale_if_match_is_conflict(env):
    status, payload = env.run(MembershipCommand(remove=("SBER",), expected=7))
    assert status == 412 and payload["code"] == "REVISION_CHANGED"
    assert env.store.desired("emulation").generation == 1 and env.store.commands()[0]["outcome"] == "conflict"


class TestChangeMembership:
    def test_answer_from_main_thread(self, env):
        def main():
            command = env.control.next_membership(2)
            env.control.answer_membership(command, 202, {"revision": 7})
        threading.Thread(target=main, daemon=True).start()
        status, payload = env.control.change_membership(MembershipCommand(add=("NG-12.26",)))
        assert status == 202 and payload["revision"] == 7 and payload["items"][0]["name"] == "SBER"

    def test_slow_main_thread_points_to_state(self, env):
        env.control._reply_timeout = 0.05
        status, payload = env.control.change_membership(MembershipCommand(add=("NG-12.26",)))
        assert status == 202 and payload["revision"] is None

    def test_conflicts(self, env):
        env.store.record_stop("emulation", "no_entries")
        status, payload = env.control.change_membership(MembershipCommand(add=("NG-12.26",)))
        assert status == 409 and payload["code"] == "STOPPING"
        env.control.mark_exited(0)
        status, payload = env.control.change_membership(MembershipCommand(add=("NG-12.26",)))
        assert status == 409 and payload["code"] == "NOT_RUNNING"

    def test_stale_if_match_is_refused_before_main_thread(self, env):
        status, payload = env.control.change_membership(MembershipCommand(add=("NG-12.26",), expected=5))
        assert status == 412 and payload["code"] == "REVISION_CHANGED"
        assert env.control.next_membership(0.01) is None

    def test_supervise_dispatches_membership_commands(self, env):
        process = SimpleNamespace(running=True)
        handled = []

        def membership(command):
            handled.append(command.add)
            env.control.answer_membership(command, 202, {"revision": 2})
            process.running = False

        live = core_module.LiveCore.__new__(core_module.LiveCore)
        live.market_store = SimpleNamespace(consumer_checkpoint=lambda consumer: None)
        threading.Thread(target=lambda: env.control.change_membership(MembershipCommand(add=("NG-12.26",))),
                         daemon=True).start()
        live._supervise(process, kind="emulation", control=env.control, poll_secs=0.05, membership=membership)
        assert handled == [("NG-12.26",)]
