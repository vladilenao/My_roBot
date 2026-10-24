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

