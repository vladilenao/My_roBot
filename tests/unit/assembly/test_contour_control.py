"""Процесс контура применяет желаемое ядра на границе тика (add-control-api)."""

import json
import logging
import sqlite3

import pytest

from src.assembly.contour_control import ContourControl, read_status
from src.assembly.core_state import CoreStateStore, Member


class Manager:
    """Менеджер сделок: режим остановки и остаток сделок."""

    def __init__(self, positions=0, entries=0):
        self.remainder = (positions, entries)
        self.calls = []

    def stop_contour(self, mode):
        self.calls.append(mode)
        return ()

    def contour_remainder(self):
        return self.remainder


@pytest.fixture
def env(tmp_path):
    core = CoreStateStore(tmp_path / "core.sqlite3")
    core.record_launch("emulation", "emulation", [{"token": "NG"}])
    stops = []

    def control():
        return ContourControl(core_db=tmp_path / "core.sqlite3", contour_id="emulation",
                              journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                              request_stop=stops.append, version="7.1.0", pid=4242)
    return core, control, stops, tmp_path


def saved(tmp_path):
    with sqlite3.connect(tmp_path / "trades.sqlite3") as conn:
        return conn.execute("SELECT applied_generation, state, stop_mode FROM contour_state").fetchone()


def test_running_contour_reports_generation_in_status(env):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    contour.before_tick()
    contour.after_tick()
    status = read_status(tmp_path / "status.json")
    assert (status["state"], status["applied_generation"], status["open_positions"]) == ("running", 1, 1)
    assert status["pid"] == 4242 and status["last_tick_at"] and stops == []
    assert manager.calls == [None]


def test_no_entries_with_open_position_drains_then_stops(env):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    core.record_stop("emulation", "no_entries")

    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopping_no_entries" and saved(tmp_path) == (2, "stopping_no_entries", "no_entries")
    assert stops == []

    # Следующий тик: остановка повторяется, позиция закрылась по стопу.
    manager.remainder = (0, 0)
    contour.before_tick()
    contour.after_tick()
    assert manager.calls == [None, "no_entries", "no_entries"]
    assert saved(tmp_path) == (2, "stopped", "no_entries") and stops == ["остановка командой ядра"]
    assert read_status(tmp_path / "status.json")["state"] == "stopped"


def test_stop_without_trades_finishes_on_first_tick(env):
    core, control, stops, _ = env
    contour = control()
    contour.start(Manager())
    core.record_stop("emulation", "close_all")
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopped" and len(stops) == 1


def test_restarted_process_applies_current_stop_again(env):
    core, control, stops, tmp_path = env
    core.record_stop("emulation", "close_all")
    first = control()
    first.start(Manager(positions=1))
    # Процесс перезапущен посреди остановки: поколение уже применено, но блок — в памяти менеджера.
    manager = Manager(positions=1)
    control().start(manager)
    assert manager.calls == ["close_all"] and saved(tmp_path) == (2, "stopping_close_all", "close_all")


def test_launch_after_stop_lifts_it(env):
    core, control, stops, _ = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    core.record_stop("emulation", "no_entries")
    contour.before_tick()
    core.record_launch("emulation", "emulation", [{"token": "BR"}])
    contour.before_tick()
    assert manager.calls[-1] is None and contour.state == "running" and contour.applied_generation == 3


def test_unreadable_core_database_keeps_last_desired(env, caplog):
    core, control, stops, tmp_path = env
    manager = Manager(positions=1)
    contour = control()
    contour.start(manager)
    (tmp_path / "core.sqlite3").write_bytes(b"not a database at all" * 100)
    with caplog.at_level(logging.WARNING, logger="src.assembly.contour_control"):
        contour.before_tick()
        contour.before_tick()
    assert contour.state == "running" and contour.applied_generation == 1
    assert sum("База ядра не читается" in record.getMessage() for record in caplog.records) == 1


def test_contour_without_core_database_trades_as_before(tmp_path):
    stops = []
    contour = ContourControl(core_db=tmp_path / "missing.sqlite3", contour_id="emulation",
                             journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                             request_stop=stops.append)
    manager = Manager()
    contour.start(manager)
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "running" and manager.calls == [] and stops == []


def test_notify_only_contour_stops_at_once(env):
    core, control, stops, _ = env
    contour = control()
    contour.start(None)
    core.record_stop("emulation", "no_entries")
    contour.before_tick()
    contour.after_tick()
    assert contour.state == "stopped" and len(stops) == 1


def test_broken_status_is_not_an_error(tmp_path):
    (tmp_path / "status.json").write_text("{broken", encoding="utf-8")
    assert read_status(tmp_path / "status.json") is None
    (tmp_path / "status.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert read_status(tmp_path / "status.json") is None
    assert read_status(tmp_path / "absent.json") is None


# ── Состав контура на границе тика (add-contour-membership) ──

SBER = {"label": "SBER", "ticker": "SBER", "instrument_type": "share", "short_name": "SBER", "uid": None, "family": None}
NG = {"label": "NG-12.26", "ticker": "NGZ6", "instrument_type": "future", "short_name": "NG-12.26", "uid": "uid-ng",
      "family": "NG"}
NG_CONTRACT = {"uid-ng": {"ticker": "uid-ng", "price_step": 0.001, "step_cost": 8.2, "go_buy": 6500.0,
                          "go_sell": 6500.0, "currency": "RUB", "expiration_date": None}}


class Roster:
    def __init__(self, keys=("SBER",)):
        self.present = set(keys)
        self.ready_keys, self.calls = set(), []
        self.live = True

    def keys(self):
        return set(self.present)

    def instrument(self, key):
        return None

    def warm(self, instrument):
        self.calls.append(("warm", instrument.ticker))
        return self.live

    def ready(self, instrument):
        return (instrument.uid or instrument.ticker) in self.ready_keys

    def admit(self, instrument, contracts=None):
        self.calls.append(("admit", instrument.ticker, sorted(contracts)))
        self.present.add(instrument.uid or instrument.ticker)

    def retire(self, instrument):
        self.calls.append(("retire", instrument.ticker))
        self.present.discard(instrument.uid or instrument.ticker)


class MemberManager(Manager):
    def __init__(self):
        super().__init__()
        self.by_key, self.instrument_calls = {}, []

    def stop_instrument(self, key, mode):
        self.instrument_calls.append((key, mode))
        return ()

    def contour_remainder(self, key=None):
        return self.by_key.get(key, (0, 0)) if key is not None else self.remainder


@pytest.fixture
def membership(tmp_path):
    core = CoreStateStore(tmp_path / "core.sqlite3")
    core.record_launch("emulation", "emulation", [SBER], members=[Member("SBER", SBER)])
    clock, stops = [0.0], []
    contour = ContourControl(core_db=tmp_path / "core.sqlite3", contour_id="emulation",
                             journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                             request_stop=stops.append, warm_timeout=600, clock=lambda: clock[0])
    manager, roster = MemberManager(), Roster()
    contour.start(manager, roster)
    return core, contour, manager, roster, clock, tmp_path


def tick(contour):
    contour.before_tick()
    contour.after_tick()


def test_added_instrument_warms_then_bootstraps_and_trades(membership):
    core, contour, manager, roster, clock, tmp_path = membership
    core.record_membership("emulation", add=[Member("uid-ng", NG, contract=NG_CONTRACT)])
    tick(contour)
    assert contour.members == {"SBER": "trading", "uid-ng": "warming"}
    tick(contour)                                                    # потребности ушли ядру, пары ещё не готовы
    assert roster.calls == [("warm", "NGZ6")] and contour.members["uid-ng"] == "warming"
    roster.ready_keys.add("uid-ng")
    tick(contour)
    assert roster.calls[-1] == ("admit", "NGZ6", ["uid-ng"]) and contour.members["uid-ng"] == "trading"
    status = read_status(tmp_path / "status.json")
    assert {item["key"]: item["state"] for item in status["members"]} == {"SBER": "trading", "uid-ng": "trading"}


def test_warming_times_out_into_overview(membership):
    core, contour, _, roster, clock, _ = membership
    core.record_membership("emulation", add=[Member("uid-ng", NG)])
    tick(contour)
    tick(contour)
    clock[0] = 600
    tick(contour)
    assert roster.calls[-1][0] == "admit" and contour.members["uid-ng"] == "trading"


def test_warm_waits_for_live_analysis(membership):
    core, contour, _, roster, clock, _ = membership
    roster.live = False
    core.record_membership("emulation", add=[Member("uid-ng", NG)])
    tick(contour)
    tick(contour)
    roster.live = True
    roster.ready_keys.add("uid-ng")
    tick(contour)
    tick(contour)
    assert [call[0] for call in roster.calls] == ["warm", "warm", "warm", "admit"]


def test_removed_instrument_drains_then_leaves(membership):
    core, contour, manager, roster, _, _ = membership
    manager.by_key["SBER"] = (1, 0)
    core.record_membership("emulation", remove=["SBER"])
    tick(contour)
    assert contour.members == {"SBER": "draining"} and manager.instrument_calls == [("SBER", "no_entries")]
    manager.by_key["SBER"] = (0, 0)
    tick(contour)
    assert contour.members == {} and roster.calls == [("retire", "SBER")]
    assert manager.instrument_calls[-1] == ("SBER", None)


def test_close_all_over_draining_and_return(membership):
    core, contour, manager, roster, _, _ = membership
    manager.by_key["SBER"] = (1, 0)
    core.record_membership("emulation", remove=["SBER"])
    tick(contour)
    core.record_membership("emulation", remove=["SBER"], mode="close_all")
    tick(contour)
    assert contour.members["SBER"] == "closing" and manager.instrument_calls[-1] == ("SBER", "close_all")
    core.record_membership("emulation", add=[Member("SBER", SBER)])
    tick(contour)
    assert contour.members["SBER"] == "trading" and manager.instrument_calls[-1] == ("SBER", None)
    assert ("warm", "SBER") not in roster.calls                    # возврат без прогрева и обзора


def test_removed_while_warming_is_dropped(membership):
    core, contour, _, roster, _, _ = membership
    core.record_membership("emulation", add=[Member("uid-ng", NG)])
    tick(contour)
    core.record_membership("emulation", remove=["uid-ng"])
    tick(contour)
    assert "uid-ng" not in contour.members and roster.calls[-1] == ("retire", "NGZ6")


def test_process_start_keeps_removed_instrument_with_open_trade(tmp_path):
    core = CoreStateStore(tmp_path / "core.sqlite3")
    core.record_launch("emulation", "emulation", [SBER], members=[Member("SBER", SBER)])
    core.record_membership("emulation", remove=["SBER"])
    manager, roster = MemberManager(), Roster()
    manager.by_key["SBER"] = (1, 0)
    contour = ContourControl(core_db=tmp_path / "core.sqlite3", contour_id="emulation",
                             journal_db=tmp_path / "trades.sqlite3", status_path=tmp_path / "status.json",
                             request_stop=lambda reason: None)
    contour.start(manager, roster)
    assert contour.members == {"SBER": "draining"} and manager.instrument_calls == [("SBER", "no_entries")]
