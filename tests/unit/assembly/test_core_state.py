"""Желаемое состояние и журнал команд ядра в core.sqlite3 (add-control-api)."""

import sqlite3

import pytest

from src.assembly.core_state import (
    ACCEPTED, CONFLICT, IGNORED, REJECTED, SCHEMA_VERSION, CoreStateStore, read_desired,
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
