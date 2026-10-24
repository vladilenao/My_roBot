"""Желаемое состояние и журнал команд ядра в core.sqlite3 (add-control-api)."""

import sqlite3

import pytest

from src.assembly.core_state import (
    ACCEPTED, CONFLICT, IGNORED, REJECTED, SCHEMA_VERSION, CoreStateStore, Member, match_members, read_desired,
)

NG = {"token": "NG", "name": "NG-12.26"}


@pytest.fixture
def store(tmp_path):
    return CoreStateStore(tmp_path / "core.sqlite3")


def test_generations_grow_with_each_command(store):
    assert store.record_launch("emulation", "emulation", [NG], source="api", command="--mode emulation") == 1
    assert store.record_stop("emulation", "no_entries").generation == 2
    assert store.record_stop("emulation", "close_all").generation == 3
    desired = store.desired("emulation")
    assert (desired.running, desired.stop_mode, desired.generation) == (False, "close_all", 3)
    assert [row["outcome"] for row in store.commands()] == [ACCEPTED, ACCEPTED, ACCEPTED]


def test_repeated_stop_is_ignored_without_new_generation(store):
    store.record_launch("emulation", "emulation", [NG])
    store.record_stop("emulation", "no_entries")
    again = store.record_stop("emulation", "no_entries")
    assert (again.outcome, again.generation) == (IGNORED, 2)
    assert store.desired("emulation").generation == 2
    assert store.commands()[0]["outcome"] == IGNORED


def test_close_all_cannot_go_back_to_no_entries(store):
    store.record_launch("emulation", "emulation", [NG])
    store.record_stop("emulation", "close_all")
    back = store.record_stop("emulation", "no_entries")
    assert back.outcome == CONFLICT and store.desired("emulation").stop_mode == "close_all"


def test_stop_before_any_launch_is_conflict(store):
    assert store.record_stop("emulation", "no_entries").outcome == CONFLICT
    assert store.desired("emulation") is None


def test_launch_after_stop_lifts_stop_with_new_generation(store):
    store.record_launch("emulation", "emulation", [NG])
    store.record_stop("emulation", "no_entries")
    assert store.record_launch("emulation", "emulation", [{"token": "BR"}]) == 3
    desired = store.desired("emulation")
    assert desired.running and desired.stop_mode is None and desired.instruments == ({"token": "BR"},)


def test_rejected_launch_is_logged_without_generation_change(store):
    store.record_launch("emulation", "emulation", [NG])
    store.record_command("emulation", "launch", {"instruments": ["AFLT"]}, REJECTED, "нет привязок")
    assert store.desired("emulation").generation == 1
    row = store.commands()[0]
    assert (row["outcome"], row["generation"], row["payload"]) == (REJECTED, 1, {"instruments": ["AFLT"]})


def test_unknown_mode_is_refused(store):
    with pytest.raises(ValueError):
        store.record_stop("emulation", "pause")


def test_state_survives_reopen_and_contour_reads_only(tmp_path):
    path = tmp_path / "core.sqlite3"
    CoreStateStore(path).record_launch("emulation", "emulation", [NG])
    reopened = CoreStateStore(path)
    assert reopened.schema_version == SCHEMA_VERSION and reopened.desired("emulation").generation == 1
    assert read_desired(path, "emulation").running
    assert read_desired(path, "account") is None
    assert read_desired(tmp_path / "missing.sqlite3", "emulation") is None
    # Соединение контура — только чтение: запись невозможна.
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE contours SET generation=99")


# ── Состав контура (add-contour-membership) ──

SBER_CARD = {"label": "SBER", "ticker": "SBER", "instrument_type": "share", "short_name": "SBER", "uid": None,
             "family": None}
NG_CARD = {"label": "NG-12.26", "ticker": "NGZ6", "instrument_type": "future", "short_name": "NG-12.26",
           "uid": "uid-ng", "family": "NG"}
BR_CARD = {"label": "BR-12.26", "ticker": "BRZ6", "instrument_type": "future", "short_name": "BR-12.26",
           "uid": "uid-br", "family": "BR"}


def member(card, contract=None):
    key = card["uid"] or card["ticker"]
    return Member(key, card, {"token": card["family"] or card["ticker"], "name": card["short_name"],
                              "instrument_type": card["instrument_type"], "days_to_expiry": None}, contract)


@pytest.fixture
def running(store):
    store.record_launch("emulation", "emulation", [SBER_CARD], members=[member(SBER_CARD)])
    return store


def states(store):
    return {m.key: (m.active, m.remove_mode, m.changed_generation) for m in store.desired("emulation").members}


def test_launch_replaces_members(running):
    running.record_launch("emulation", "emulation", [NG_CARD], members=[member(NG_CARD)])
    assert states(running) == {"uid-ng": (True, None, 2)}


def test_add_and_remove_raise_generation_once(running):
    outcome = running.record_membership("emulation", add=[member(NG_CARD, {"uid-ng": {"price_step": 0.001}}),
                                                         member(BR_CARD)])
    assert (outcome.outcome, outcome.generation) == (ACCEPTED, 2)
    assert running.desired("emulation").members[1].contract == {"uid-ng": {"price_step": 0.001}}
    outcome = running.record_membership("emulation", remove=["SBER"])
    assert (outcome.outcome, outcome.generation) == (ACCEPTED, 3)
    assert states(running)["SBER"] == (False, "no_entries", 3)
    assert [m.key for m in running.desired("emulation").active] == ["uid-ng", "uid-br"]


def test_repeats_are_ignored(running):
    assert running.record_membership("emulation", add=[member(SBER_CARD)]).outcome == IGNORED
    running.record_membership("emulation", remove=["SBER"])
    again = running.record_membership("emulation", remove=["SBER"], mode="no_entries")
    assert (again.outcome, again.generation) == (IGNORED, 2)


def test_remove_mode_transitions(running):
    running.record_membership("emulation", remove=["SBER"])
    assert running.record_membership("emulation", remove=["SBER"], mode="close_all").generation == 3
    back = running.record_membership("emulation", remove=["SBER"], mode="no_entries")
    assert back.outcome == CONFLICT and states(running)["SBER"] == (False, "close_all", 3)


def test_add_returns_removed_member(running):
    running.record_membership("emulation", remove=["SBER"])
    outcome = running.record_membership("emulation", add=[member(SBER_CARD)])
    assert outcome.generation == 3 and states(running)["SBER"] == (True, None, 3)


def test_membership_needs_running_contour(running):
    running.record_stop("emulation", "no_entries")
    assert running.record_membership("emulation", add=[member(NG_CARD)]).outcome == CONFLICT
    assert CoreStateStore(running.path).record_membership("other", add=[member(NG_CARD)]).outcome == CONFLICT


def test_unknown_remove_key_changes_nothing(running):
    outcome = running.record_membership("emulation", add=[member(NG_CARD)], remove=["GAZP"])
    assert outcome.outcome == CONFLICT and "uid-ng" not in states(running)


def test_contour_launched_before_membership_gets_members_from_cards(store):
    store.record_launch("emulation", "emulation", [SBER_CARD], members=[])
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM contour_members")
    assert store.record_membership("emulation", add=[member(NG_CARD)]).generation == 2
    assert set(states(store)) == {"SBER", "uid-ng"}


def test_match_members_by_name_ticker_and_family():
    members = [member(SBER_CARD), member(NG_CARD), member(BR_CARD)]
    keys, rejections = match_members(members, ["sber", "NG", "BRZ6", "GAZP", "NG-12.26"])
    assert keys == ["SBER", "uid-ng", "uid-br"]
    assert rejections == [("GAZP", "Инструмента нет в составе контура.")]
    two = members + [member({**NG_CARD, "uid": "uid-ng2", "short_name": "NG-03.27", "ticker": "NGH7"})]
    assert "несколько контрактов" in match_members(two, ["NG"])[1][0][1]


def test_contour_reads_members_read_only(running, tmp_path):
    running.record_membership("emulation", add=[member(NG_CARD)])
    desired = read_desired(running.path, "emulation")
    assert [(m.key, m.active) for m in desired.members] == [("SBER", True), ("uid-ng", True)]
