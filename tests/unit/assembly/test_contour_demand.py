"""Ядро применяет потребности контуров из рыночной базы (add-contour-process, D3)."""

import json
from datetime import datetime, timezone

import pytest

from src.assembly import core as core_module
from src.assembly.contracts import instrument_payload
from src.assembly.demand import ContourDemandPoller, merge_demand, merge_obligations
from src.data.market_store import MarketDataStore
from src.instruments import Instrument

NG = Instrument("NG-12.26", "NGZ6", "future", "NG-12.26", None, "NG")
SBER = Instrument("SBER", "SBER", "share", "SBER", "uid-sber")
T10 = datetime(2026, 10, 23, 10, 0)


class FakeCache:
    def __init__(self):
        self.history, self.lowers = [], []

    def request_live_history(self, instrument, timeframe, min_bars):
        self.history.append((instrument.ticker, timeframe, min_bars))

    def note_execution_lower(self, instrument, lower):
        self.lowers.append((instrument.ticker, lower))

    def pair_uid(self, instrument, timeframe="1m"):
        return {"NGZ6": "uid-ng"}.get(instrument.ticker, instrument.uid)


class FakeWorker:
    def __init__(self):
        self.snapshots = []

    def set_execution_obligations(self, obligations):
        self.snapshots.append(dict(obligations))


def test_merge_obligations_earliest_then_inclusive():
    merged = merge_obligations({
        "a": {"uid-ng": (T10, False), "uid-sber": (datetime(2026, 10, 23, 11, 0), False)},
        "b": {"uid-ng": (T10, True), "uid-sber": (datetime(2026, 10, 23, 9, 0), False)},
    })

    assert merged == {"uid-ng": (T10, True), "uid-sber": (datetime(2026, 10, 23, 9, 0), False)}


def test_merge_demand_keeps_deepest():
    rows = [{"instrument_key": "uid-sber", "interval": "15m", "min_bars": 120, "instrument": instrument_payload(SBER)},
            {"instrument_key": "uid-sber", "interval": "15m", "min_bars": 200, "instrument": instrument_payload(SBER)}]

    (instrument, depth), = merge_demand(rows).values()

    assert instrument == SBER and depth == 200


def _poller(tmp_path, *, tracks=True):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache, worker = FakeCache(), FakeWorker()
    poller = ContourDemandPoller(store, cache, worker=worker, instruments=[NG, SBER], tracks_obligations=tracks)
    return store, cache, worker, poller


def test_new_pair_is_warmed_once_and_again_when_deeper(tmp_path):
    store, cache, _, poller = _poller(tmp_path)
    store.record_contour_demand("emulation", [("uid-sber", "15m", 120, instrument_payload(SBER))])

    poller.poll_once()
    poller.poll_once()
    store.record_contour_demand("emulation", [("uid-sber", "15m", 200, instrument_payload(SBER))])
    poller.poll_once()

    assert cache.history == [("SBER", "15m", 120), ("SBER", "15m", 200)]


def test_obligations_reach_owner_and_execution_lower(tmp_path):
    store, cache, worker, poller = _poller(tmp_path)
    store.replace_contour_obligations("emulation", {"uid-ng": (T10, True)})

    poller.poll_once()
    poller.poll_once()

    assert worker.snapshots == [{"uid-ng": (T10, True)}]
    assert cache.lowers == [("NGZ6", T10)]


def test_stopped_contour_rows_keep_working(tmp_path):
    store, _, worker, poller = _poller(tmp_path)
    store.replace_contour_obligations("emulation", {"uid-ng": (T10, False)})
    # Процесс контура завершился: строки остались, ядро продолжает их применять.

    poller.poll_once()

    assert worker.snapshots[-1] == {"uid-ng": (T10, False)}


def test_notify_only_does_not_apply_obligations(tmp_path):
    store, _, worker, poller = _poller(tmp_path, tracks=False)
    store.replace_contour_obligations("emulation", {"uid-ng": (T10, False)})

    poller.poll_once()

    assert worker.snapshots == []


class FakeProcess:
    instances = []

    def __init__(self, kind, *, on_exit):
        self.kind, self.on_exit, self.running, self.exit_code, self.stopped = kind, on_exit, False, None, False
        FakeProcess.instances.append(self)

    def start(self):
        self.running = True
        return 4321

    def stop(self):
        self.stopped, self.running, self.exit_code = True, False, 0
        self.on_exit(0, True)
        return 0


def test_run_contour_writes_launch_without_secrets_and_tracks_state(tmp_path, monkeypatch):
    from src.bot.launch_control import LaunchControl
    from src.portfolio.models import ContractMeta

    monkeypatch.setenv("SECRET_PROBE", "x")
    monkeypatch.setattr(core_module, "TINKOFF_TOKEN", "t.tinkoff-secret-value")
    monkeypatch.setattr(core_module, "_load_contracts_metadata", lambda instruments, **kw: {
        "SBER": ContractMeta("SBER", 0.01, 0.1, 0.0, 0.0)})
    monkeypatch.setattr(core_module, "trading_enabled", lambda: True)
    live = core_module.LiveCore.__new__(core_module.LiveCore)
    live.state_dir = tmp_path
    live.client_provider = None
    live.request_budget = None
    live.market_store = MarketDataStore(tmp_path / "market.sqlite3")
    live.data_cache = FakeCache()
    live._worker = FakeWorker()
    live._cluster_consumer = None
    served = []
    monkeypatch.setattr(core_module.LiveCore, "serve", lambda self, demand: served.append(demand) or None)
    monkeypatch.setattr(core_module.LiveCore, "start_clusters", lambda self: None)
    control = LaunchControl()
    control.mark_launched("api", [], "--mode emulation --instruments SBER")
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            producer = live.market_store.source_info().producer_id
            live.market_store.acknowledge("contour-emulation", producer, 0)
        elif len(sleeps) == 2:
            assert control.snapshot()["state"] == "running" and control.snapshot()["pid"] == 4321
            raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)
    # Надзор ждёт команду состава вместо паузы (add-contour-membership).
    monkeypatch.setattr(LaunchControl, "next_membership", lambda self, timeout: fake_sleep(timeout))
    FakeProcess.instances.clear()

    code = live.run_contour([SBER], kind="emulation", control=control, source="api",
                            command="--mode emulation --instruments SBER", process_factory=FakeProcess, poll_secs=0)

    text = (tmp_path / "contours" / "emulation" / "launch.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert code == 0 and FakeProcess.instances[0].stopped
    assert payload["contour_id"] == "emulation" and payload["telegram_channel_env"] == "TELEGRAM_CHANNEL_ID"
    assert payload["instruments"][0]["ticker"] == "SBER" and payload["contracts"]["SBER"]["price_step"] == 0.01
    assert "tinkoff-secret-value" not in text
    assert served[0].instruments == (SBER,) and served[0].obligations == {}
    assert control.snapshot()["state"] == "stopped"


@pytest.mark.parametrize("stamp, expected", [("2026-10-23T10:00:01Z", True), ("2026-10-23T09:59:59Z", False)])
def test_after(stamp, expected):
    assert core_module._after(stamp, datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)) is expected



class ExitingProcess(FakeProcess):
    """Процесс контура, который сам завершается после старта: остановка командой выполнена."""

    def start(self):
        self.exit_code = 0
        self.on_exit(0, False)
        return 4300 + len(FakeProcess.instances)


def test_relaunch_after_stop_starts_new_process_without_second_serve(tmp_path, monkeypatch):
    from src.assembly.core_state import CoreStateStore
    from src.bot.launch_control import LaunchControl
    from src.portfolio.models import ContractMeta

    monkeypatch.setattr(core_module, "_load_contracts_metadata", lambda instruments, **kw: {
        item.ticker: ContractMeta(item.ticker, 0.01, 0.1, 0.0, 0.0) for item in instruments})
    monkeypatch.setattr(core_module, "trading_enabled", lambda: True)
    live = core_module.LiveCore.__new__(core_module.LiveCore)
    live.state_dir = tmp_path
    live.client_provider = None
    live.request_budget = None
    live.market_store = MarketDataStore(tmp_path / "market.sqlite3")
    live.data_cache = FakeCache()
    live._worker = FakeWorker()
    live._cluster_consumer = None
    pairs = []
    live._pattern_trigger = type("Trigger", (), {"add_pairs": lambda self, items: pairs.extend(items)})()
    served = []
    monkeypatch.setattr(core_module.LiveCore, "serve", lambda self, demand: served.append(demand) or None)
    monkeypatch.setattr(core_module.LiveCore, "start_clusters", lambda self: None)
    monkeypatch.setattr(core_module, "_pattern_pairs", lambda instruments: tuple((item, "1h") for item in instruments))
    store = CoreStateStore(tmp_path / "core.sqlite3")
    control = LaunchControl(core_state=store, contour_dir=tmp_path / "contours" / "emulation")
    launches = iter([([NG], "api", "--mode emulation --instruments NG-12.26"), None])

    def next_launch():
        assert control.snapshot()["state"] == "stopped"
        return next(launches)

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", interrupt)
    FakeProcess.instances.clear()

    code = live.run_contour([SBER], kind="emulation", control=control, source="cli", command="--mode emulation",
                            core_state=store, next_launch=next_launch, process_factory=ExitingProcess, poll_secs=0)

    assert code == 0 and len(served) == 1 and len(FakeProcess.instances) == 2
    payload = json.loads((tmp_path / "contours" / "emulation" / "launch.json").read_text(encoding="utf-8"))
    assert [item["ticker"] for item in payload["instruments"]] == ["NGZ6"] and payload["generation"] == 2
    assert [(row["action"], row["generation"]) for row in reversed(store.commands())] == [("launch", 1), ("launch", 2)]
    assert store.desired("emulation").instruments[0]["ticker"] == "NGZ6"
    assert pairs == [(NG, "1h")]


# ── Снятие пар без спроса (add-contour-membership) ──

class ReleasingCache(FakeCache):
    def __init__(self):
        super().__init__()
        self.unregistered = []

    def unregister_live_pair(self, uid, timeframe):
        self.unregistered.append((uid, timeframe))
        return True


class RosterWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.retired = []

    def retire_roster_pairs(self, pairs):
        self.retired.extend(pairs)


def _releasing(tmp_path, *, tracks=True):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    cache, worker, clock, released = ReleasingCache(), RosterWorker(), [0.0], []
    poller = ContourDemandPoller(store, cache, worker=worker, instruments=[NG, SBER], tracks_obligations=tracks,
                                 release_seconds=900, on_release=lambda item, tf: released.append((item.ticker, tf)),
                                 clock=lambda: clock[0])
    return store, cache, worker, poller, clock, released


def test_pair_without_demand_is_released_after_grace(tmp_path):
    store, cache, worker, poller, clock, released = _releasing(tmp_path)
    store.record_contour_demand("emulation", [("uid-sber", "15m", 120, instrument_payload(SBER))])
    poller.poll_once()
    store.remove_contour_demand("emulation", "uid-sber")
    poller.poll_once()
    clock[0] = 899
    poller.poll_once()
    assert cache.unregistered == []
    clock[0] = 900
    poller.poll_once()
    assert cache.unregistered == [("uid-sber", "15m")] and worker.retired == [("uid-sber", "15m")]
    assert released == [("SBER", "15m")]
    poller.poll_once()
    assert cache.unregistered == [("uid-sber", "15m")]           # однократно


def test_returned_demand_cancels_release(tmp_path):
    store, cache, _, poller, clock, _ = _releasing(tmp_path)
    row = [("uid-sber", "15m", 120, instrument_payload(SBER))]
    store.record_contour_demand("emulation", row)
    poller.poll_once()
    store.replace_contour_demand("emulation", [])                  # запуск контура стёр спрос
    poller.poll_once()
    clock[0] = 60
    store.record_contour_demand("emulation", row)                  # обзор вернул спрос
    poller.poll_once()
    clock[0] = 2000
    poller.poll_once()
    assert cache.unregistered == [] and cache.history == [("SBER", "15m", 120)]


def test_obligation_keeps_pair(tmp_path):
    store, cache, _, poller, clock, _ = _releasing(tmp_path)
    store.record_contour_demand("emulation", [("uid-ng", "1m", 1, instrument_payload(NG))])
    poller.poll_once()
    store.remove_contour_demand("emulation", "uid-ng")
    store.replace_contour_obligations("emulation", {"uid-ng": (T10, True)})
    poller.poll_once()
    clock[0] = 5000
    poller.poll_once()
    assert cache.unregistered == []


def test_replace_demand_drops_previous_launch_rows(tmp_path):
    from src.assembly.feed import StoreCoreLink

    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.record_contour_demand("emulation", [("uid-br", "5m", 50, instrument_payload(NG))])
    store.record_contour_demand("account", [("uid-br", "5m", 50, instrument_payload(NG))])
    link = StoreCoreLink(store, "emulation")
    link.replace([(SBER, "1h", 120), (SBER, "1h", 60)])
    assert [(row["contour_id"], row["instrument_key"], row["min_bars"]) for row in store.contour_demands()] == [
        ("account", "uid-br", 50), ("emulation", "uid-sber", 120)]
    link.request_history(SBER, "1h", 100)                          # уже отправлено глубже — без записи
    link.forget(SBER)
    assert [row["contour_id"] for row in store.contour_demands()] == ["account"]
    link.request_history(SBER, "1h", 100)                          # после выхода — снова пишется
    assert len(store.contour_demands()) == 2
