"""Судьба контура при старте ядра: подхват, замена версии, автоподъём (add-contour-recovery)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.assembly import core as core_module
from src.assembly import recovery
from src.assembly.contour_control import write_status
from src.assembly.core_state import CoreStateStore, Member
from src.assembly.process import LAUNCH_FILE, AdoptedProcess
from src.bot.launch import Chosen, SOURCE_API
from src.bot.launch_control import LaunchControl
from src.portfolio.models import ContractMeta

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
NG = ("NG-12.26", "NGZ6", "future", "NG-12.26")
SBER = ("SBER", "SBER", "share", "SBER")


def _stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def env(tmp_path):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    directory = tmp_path / "contours" / "emulation"
    directory.mkdir(parents=True)
    members = [core_module._member(core_module._normalized(item), {}, [
        Chosen("NG", "NG-12.26", "future", 52).payload(), Chosen("SBER", "SBER", "share").payload()])
        for item in (NG, SBER)]
    store.record_launch("emulation", "emulation", [], source="api", command="--mode emulation --instruments NG,SBER",
                        members=members)

    def status(**fields):
        payload = {"contour_id": "emulation", "state": "running", "pid": 4242, "version": "7.1.0",
                   "alive_at": _stamp(NOW - timedelta(seconds=5)), "exited_at": None,
                   "members": [{"key": "NGZ6", "state": "trading"}], **fields}
        write_status(directory / "status.json", payload)

    def plan(*, alive=(), explicit=False, autostart=True):
        return recovery.plan_recovery(directory, version="7.1.0", desired=store.desired("emulation"),
                                      explicit=explicit, autostart=autostart, timeout=30,
                                      alive=lambda pid: pid in alive, now=NOW)
    return SimpleNamespace(store=store, directory=directory, status=status, plan=plan)


def _pid(env, pid=4242):
    (env.directory / "robot.pid").write_text(str(pid), encoding="utf-8")


class TestPlan:
    def test_live_contour_of_same_version_is_adopted(self, env):
        _pid(env)
        env.status()
        result = env.plan(alive={4242}, explicit=True)
        assert (result.action, result.pid) == (recovery.ADOPT, 4242)

    def test_live_contour_of_other_version_is_replaced(self, env):
        _pid(env)
        env.status(version="7.0.0")
        assert env.plan(alive={4242}).action == recovery.REPLACE

    @pytest.mark.parametrize("fields", [
        {"alive_at": _stamp(NOW - timedelta(seconds=31))},   # завис или чужой процесс с тем же pid
        {"pid": 5151},                                       # снимок другого процесса
        {"alive_at": None},                                  # снимок версии до пульса
    ])
    def test_unconfirmed_live_pid_is_blocked(self, env, fields):
        _pid(env)
        env.status(**fields)
        assert env.plan(alive={4242}).action == recovery.BLOCKED

    def test_exiting_contour_is_waited(self, env):
        _pid(env)
        env.status(exited_at=_stamp(NOW))
        assert env.plan(alive={4242}).action == recovery.REPLACE

    def test_dead_pid_falls_through(self, env):
        _pid(env)
        env.status()
        assert env.plan(alive=()).action == recovery.AUTOSTART
        assert env.plan(alive=(), explicit=True).action == recovery.LAUNCH

    def test_autostart_switch_and_stopped_contour(self, env):
        env.status()
        assert env.plan(autostart=False).action == recovery.WAIT
        env.store.record_stop("emulation", "no_entries")
        env.status(state="stopping_no_entries")
        assert env.plan().action == recovery.AUTOSTART       # остановка не дожила — довести
        env.status(state="stopped")
        assert env.plan().action == recovery.WAIT

    def test_never_launched(self, tmp_path):
        result = recovery.plan_recovery(tmp_path, version="7.1.0", desired=None, explicit=False, autostart=True,
                                        timeout=30, alive=lambda pid: False, now=NOW)
        assert result.action == recovery.WAIT


class TestAutostartMembers:
    def test_removed_member_kept_only_while_contour_held_it(self, env):
        env.store.record_membership("emulation", remove=["SBER"])
        desired = env.store.desired("emulation")
        held = {"members": [{"key": "NGZ6"}, {"key": "SBER"}]}
        assert [m.key for m in recovery.autostart_members(desired, held)] == ["NGZ6", "SBER"]
        assert [m.key for m in recovery.autostart_members(desired, {"members": [{"key": "NGZ6"}]})] == ["NGZ6"]
        # Без снимка — все: контур снимет лишних на первом тике.
        assert [m.key for m in recovery.autostart_members(desired, None)] == ["NGZ6", "SBER"]

    def test_plan_gives_instruments(self, env):
        env.status()
        assert [item.ticker for item in env.plan().instruments()] == ["NGZ6", "SBER"]


class TestAdoptedProcess:
    def test_exit_is_reported_without_code(self):
        state = {"alive": True}
        exits = []
        process = AdoptedProcess("emulation", 4242, on_exit=lambda code, stopping: exits.append((code, stopping)),
                                 alive=lambda pid: state["alive"], poll_secs=0.01)
        assert process.start() == 4242 and process.running and process.pid == 4242
        state["alive"] = False
        process._watcher.join(timeout=2)
        assert not process.running and process.pid is None and exits == [(None, False)]

    def test_stop_waits_then_kills(self):
        killed = []
        process = AdoptedProcess("emulation", 4242, alive=lambda pid: not killed, kill=killed.append,
                                 poll_secs=0.01, stop_timeout=0.05, sleep=lambda secs: None)
        process.start()
        process.stop()
        assert killed == [4242] and not process.running


class TestCore:
    @pytest.fixture
    def live(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_module, "_load_contracts_metadata", lambda instruments, **kw: {
            item.ticker: ContractMeta(item.ticker, 0.01, 1.0, 100.0, 100.0)
            for item in instruments if item.ticker == "SBER"})
        live = core_module.LiveCore.__new__(core_module.LiveCore)
        live.state_dir = tmp_path
        live.client_provider = live.request_budget = None
        return live

    def test_autostart_keeps_generation_and_falls_back_to_stored_cards(self, env, live, tmp_path):
        from src.assembly.contracts import contracts_payload

        stored = contracts_payload({"NGZ6": ContractMeta("NGZ6", 0.001, 8.2, 6500.0, 6500.0)})
        ng, sber = env.store.desired("emulation").members
        env.store.record_launch("emulation", "emulation", [], source="api",
                                members=[Member(ng.key, ng.instrument, ng.chosen, stored), sber])
        desired = env.store.desired("emulation")
        started = []
        factory = lambda kind, on_exit: SimpleNamespace(start=lambda: started.append(kind) or 777)
        live._start_contour_process(recovery.RecoveryPlan(recovery.AUTOSTART, members=desired.members).instruments(),
                                    kind="emulation", control=None, source="", command="",
                                    core_state=env.store, process_factory=factory, desired=desired)
        launch = json.loads((tmp_path / "contours" / "emulation" / LAUNCH_FILE).read_text(encoding="utf-8"))
        assert env.store.desired("emulation").generation == desired.generation == launch["generation"]
        assert set(launch["contracts"]) == {"NGZ6", "SBER"} and launch["source"] == "api" and started == ["emulation"]

    def test_launch_recorded_by_command_is_not_recorded_again(self, live, tmp_path):
        """rework-contour-api: команда записала целевое «работает»; ядро берёт поколение и сохраняет карточки."""
        store = CoreStateStore(tmp_path / "core.sqlite3")
        control = LaunchControl(core_state=store)
        chosen = [Chosen("SBER", "SBER", "share")]
        assert control.mark_launched(SOURCE_API, chosen, '{"mode": "on", "instruments": ["SBER"]}', instruments=[SBER])
        assert store.desired("emulation").members[0].contract is None
        started = []
        process = SimpleNamespace(start=lambda: started.append("emulation") or 777)
        live._start_contour_process([core_module._normalized(SBER)], kind="emulation", control=control, source="api",
                                    command="", core_state=store, process_factory=lambda kind, on_exit: process,
                                    desired=store.desired("emulation") if control.launch_recorded() else None)
        launch = json.loads((tmp_path / "contours" / "emulation" / LAUNCH_FILE).read_text(encoding="utf-8"))
        desired = store.desired("emulation")
        assert desired.generation == launch["generation"] == 1 and started == ["emulation"]
        assert desired.members[0].contract["SBER"]["price_step"] == 0.01
        assert not control.launch_recorded()

    @pytest.mark.parametrize("exited, state", [(True, "stopped"), (False, "failed")])
    def test_adopted_exit_is_judged_by_status(self, env, live, monkeypatch, exited, state):
        control = LaunchControl(core_state=env.store, contour_dir=env.directory)
        processes = []

        class Fake:
            def __init__(self, kind, pid, *, on_exit, poll_secs):
                self.on_exit = on_exit
                processes.append(self)

            def start(self):
                return 4242

        monkeypatch.setattr("src.assembly.process.AdoptedProcess", Fake)
        live._adopt_contour_process(4242, kind="emulation", control=control, poll_secs=0.01)
        assert control.snapshot()["pid"] == 4242
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        env.status(exited_at=_stamp(later) if exited else None)
        processes[0].on_exit(None, False)
        snapshot = control.snapshot()
        assert (snapshot["state"], snapshot["exit_code"]) == (state, 0 if exited else None)


def test_recovered_contour_shows_source_from_desired(env):
    control = LaunchControl(core_state=env.store, contour_dir=env.directory)
    control.mark_recovered(recovery.RECOVERY_ADOPTED, env.store.desired("emulation"))
    control.mark_process(4242)
    view = control.emulation()
    assert (view["state"], view["launch"]["recovery"], view["launch"]["source"]) == ("starting", "adopted", SOURCE_API)
    assert [item["name"] for item in view["instruments"]] == ["NG-12.26", "SBER"]
    control.mark_launched(SOURCE_API, [], "--mode emulation")
    assert control.emulation()["launch"]["recovery"] is None


class TestCoreStart:
    """run.py: решение о контуре, когда ядро готово (робот всегда стартует в ожидании команды)."""

    @pytest.fixture
    def launch(self, monkeypatch, tmp_path):
        import os

        import run

        calls = {}

        class FakeCore:
            def __init__(self, state_dir, client_provider):
                self.early = SimpleNamespace(request_budget=None)
                self.export_server = None
                self.pulse = self.starter = None

            def start_control_api(self, control):
                self.export_server = SimpleNamespace(address=("127.0.0.1", 8101), close=lambda: None)
                return self.export_server

            def core_snapshot(self):
                return self.starter.payload()

            def start_market(self):
                return self

            def reset_market(self):
                pass

            def ensure_market_data(self):
                return self

            def run_contour(self, instruments, *, recovery=None, **kwargs):
                calls["run"] = ([item.ticker for item in instruments], recovery and recovery.action)
                return 0

            def close(self):
                if self.pulse is not None:
                    self.pulse.stop()

        def command(*args, **kwargs):
            calls["choose"] = True
            return [NG]

        monkeypatch.setattr(run, "LiveCore", FakeCore)
        monkeypatch.setattr(run, "acquire_runtime_lock", lambda state_dir: None)
        monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
        monkeypatch.setattr(run, "ROBOT_CONTROL_TOKEN", "control-test-key")
        monkeypatch.setattr(run, "_await_command", command)
        store = CoreStateStore(tmp_path / "core.sqlite3")
        store.record_launch("emulation", "emulation", [], source="api",
                            members=[core_module._member(core_module._normalized(NG), {}, [])])
        (tmp_path / "contours" / "emulation").mkdir(parents=True)

        def start():
            return run._run_robot(tmp_path, client_provider=object())
        return SimpleNamespace(start=start, calls=calls, store=store, run=run, own_pid=os.getpid(),
                               directory=tmp_path / "contours" / "emulation", tmp_path=tmp_path)

    def test_start_autostarts_without_command(self, launch):
        assert launch.start() == 0
        assert launch.calls == {"run": (["NGZ6"], recovery.AUTOSTART)}
        assert launch.store.heartbeat().stopped                     # робот закрылся — пульс «остановлено»

    def test_autostart_off_waits_for_command(self, launch, monkeypatch):
        monkeypatch.setattr(launch.run, "CONTOURS_AUTOSTART", False)
        assert launch.start() == 0
        assert launch.calls == {"choose": True, "run": (["NGZ6"], None)}

    def test_without_control_key_robot_does_not_start(self, launch, monkeypatch):
        monkeypatch.setattr(launch.run, "ROBOT_CONTROL_TOKEN", "")
        assert launch.start() == 2
        assert launch.calls == {}

    def test_unconfirmed_pid_keeps_robot_and_waits_for_free_folder(self, launch, capsys):
        (launch.directory / "robot.pid").write_text(str(launch.own_pid), encoding="utf-8")
        assert launch.run._contour_busy(launch.tmp_path) == launch.own_pid
        assert launch.start() == 0
        out = capsys.readouterr().out
        assert f"pid {launch.own_pid}" in out and "Эмуляция не запускается, пока папка занята" in out
        assert launch.calls == {"choose": True, "run": (["NGZ6"], None)}

    def test_other_version_is_waited_then_killed_then_autostarted(self, launch, monkeypatch):
        from src.assembly import process

        plans = iter([recovery.RecoveryPlan(recovery.REPLACE, 5151, {"version": "7.0.0"}),
                      recovery.RecoveryPlan(recovery.WAIT)])
        monkeypatch.setattr(recovery, "plan_recovery", lambda *a, **k: next(plans))
        waits, killed = [], []
        monkeypatch.setattr(process, "wait_exit", lambda pid, timeout: waits.append(pid) or len(waits) > 1)
        monkeypatch.setattr(process, "terminate_pid", killed.append)
        result = launch.run._recover_contour(launch.directory.parents[1], launch.store, None)
        assert result.action == recovery.WAIT and waits == [5151, 5151] and killed == [5151]
