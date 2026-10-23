"""Раздельная сборка ядра и торгового контура (refactor-core-contour-split)."""

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import run
from src.assembly import contour, core
from src.assembly.history import HistoricalMarketServices
from src.assembly.services import ContourDemand
from src.instruments import Instrument

ROOT = Path(__file__).resolve().parents[3]
SBER = Instrument("SBER", "SBER", "share", "SBER", "uid-sber")
NG = Instrument("NG", "NGZ6", "future", "NG-12.26", "uid-ng")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", ["core.py", "history.py", "services.py"])
def test_services_do_not_import_contour(module):
    imported = _imports(ROOT / "src" / "assembly" / module)
    assert not any("contour" in name for name in imported), sorted(imported)


def test_feed_services_do_not_import_core_or_history():
    imported = _imports(ROOT / "src" / "assembly" / "feed.py")
    assert not {"src.assembly.core", "src.assembly.history", "src.assembly.contour"} & imported, sorted(imported)


def test_contour_reaches_data_only_through_services_port():
    imported = _imports(ROOT / "src" / "assembly" / "contour.py")
    assert "src.assembly.core" not in imported and "src.assembly.history" not in imported


class TestBuildDemand:
    def test_notify_only_has_no_obligations(self, monkeypatch):
        monkeypatch.setattr(contour, "SHARE_STRATEGIES", {"SBER": [SimpleNamespace(timeframe="1h")]})
        demand = contour.build_demand(contour._Runtime(), [SBER], data_cache=None)
        assert demand == ContourDemand((SBER,), pattern_pairs=((SBER, "1h"),))

    def test_pattern_pairs_are_assignment_timeframes_of_selected_instruments(self, monkeypatch):
        monkeypatch.setattr(contour, "SHARE_STRATEGIES", {"SBER": [SimpleNamespace(timeframe="4h"),
                                                                   SimpleNamespace(timeframe="1h"),
                                                                   SimpleNamespace(timeframe="1h")]})
        monkeypatch.setattr(contour, "FUTURE_STRATEGIES", {"NG": [SimpleNamespace(timeframe="15m")]})
        demand = contour.build_demand(contour._Runtime(), [SBER, NG, ("GAZP", "GAZP", "share")], data_cache=None)
        assert [(item.ticker, tf) for item, tf in demand.pattern_pairs] == [("SBER", "1h"), ("SBER", "4h"),
                                                                           ("NGZ6", "15m")]

    def test_obligations_requests_and_lower_come_from_contour_storage(self):
        plan = SimpleNamespace(instrument_id="uid-ng", timeframe="1h", profile=SimpleNamespace(parameters={"atr_period": 20}))
        lower = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)
        storage = SimpleNamespace(load_trades=lambda: (SimpleNamespace(plan=plan),),
                                  execution_bar_boundaries=lambda: {"uid-ng": (lower, False)})
        cache = SimpleNamespace(pair_uid=lambda instrument, timeframe: instrument.uid,
                                instrument_uid=lambda instrument: instrument.uid)
        runtime = SimpleNamespace(storage=storage, contract_lifecycle=None)
        demand = contour.build_demand(runtime, [NG, SBER], cache)
        assert demand.instruments == (NG, SBER)
        assert demand.aliases["NGZ6"] == "uid-ng"
        # Сопровождение держит ТФ плана с прогревом ATR и базовую 1m защиты.
        assert set(demand.history_requests) == {(NG, "1h", 21), (NG, "1m", 1)}
        assert demand.execution_lower[0][0] is NG
        assert demand.obligations is not None
        # Алиасы остаются у торговой части контура, ядро их в контур не пишет.
        assert runtime.execution_aliases == demand.aliases and runtime.execution_instruments == [NG, SBER]

    def test_lifecycle_roles_extend_roster(self):
        old = Instrument("NG", "NGU6", "future", "NG-9.26", "old")
        runtime = SimpleNamespace(storage=None, contract_lifecycle=SimpleNamespace(
            execution_instruments=lambda: [old], market_instruments=lambda: [NG]))
        assert contour.build_demand(runtime, [NG], None).instruments == (NG, old)


def test_historical_services_start_no_live_parts(monkeypatch, tmp_path):
    built = SimpleNamespace(set_boundary_checks=lambda checks: None)
    monkeypatch.setattr(core, "_build_market_data", lambda **kw: (object(), None, None, None, None, built))
    for name in ("_start_early_market_data", "_start_live_ingestion", "_start_local_api"):
        monkeypatch.setattr(core, name, lambda *a, **k: pytest.fail("история запустила живую часть"))
    services = HistoricalMarketServices(tmp_path, clock=object(), client_provider=object())
    assert services.serve(ContourDemand(())) is None
    assert not hasattr(services, "pattern_sink") and not services.tracks_obligations
    services.update_obligations(object())
    services.close()


def test_live_core_closes_in_previous_order():
    order = []
    live = core.LiveCore.__new__(core.LiveCore)
    live._pattern_trigger = SimpleNamespace(stop=lambda: order.append("trigger"))
    live.pattern_sink = SimpleNamespace(stop=lambda: order.append("patterns"))
    live._poller = SimpleNamespace(close=lambda: order.append("poller"))
    live.early = None
    live._cluster_consumer = SimpleNamespace(stop=lambda: order.append("clusters"))
    live.export_server = SimpleNamespace(close=lambda: order.append("api"))
    live.close()
    assert order == ["trigger", "patterns", "poller", "clusters", "api"]


def test_live_core_without_serve_closes_early_lanes():
    order = []
    live = core.LiveCore.__new__(core.LiveCore)
    live.pattern_sink = live._pattern_trigger = live._poller = live._cluster_consumer = None
    live.early = SimpleNamespace(poller=SimpleNamespace(close=lambda: order.append("early")))
    live.export_server = SimpleNamespace(close=lambda: order.append("api"))
    live.close()
    assert order == ["early", "api"]


@pytest.mark.parametrize("market_feed", [False, True])
def test_launch_keeps_start_and_stop_order(monkeypatch, tmp_path, market_feed):
    order = []

    class FakeCore:
        def __init__(self, state_dir, client_provider):
            self.early = SimpleNamespace(request_budget=None)
            self.export_server = None

        def start_early(self):
            order.append("early")

        def start_api(self, control=None):
            order.append("api")

        def ensure_market_data(self):
            order.append("data")
            return self

        def serve(self, demand):
            order.append("serve")
            return "waiter"

        def close(self):
            order.append("core.close")

    class FakeFeed:
        def __init__(self, core, *, contour_id, poll_seconds):
            assert isinstance(core, FakeCore) and contour_id == "emulation"
            order.append("feed")
            self.core = core

        def serve(self, demand):
            return self.core.serve(demand)

        def close(self):
            order.append("feed.close")
            self.core.close()

    services_class = FakeFeed if market_feed else FakeCore

    class FakeContour:
        def __init__(self, settings, services):
            assert settings.kind == "emulation" and services.__class__ is services_class

        def build(self):
            order.append("build")
            return self

        def demand(self):
            order.append("demand")

        def run(self, waiter):
            order.append(f"run:{waiter}")

        def finish(self):
            order.append("finish")

        def close(self):
            order.append("contour.close")

    monkeypatch.setattr(run, "LiveCore", FakeCore)
    monkeypatch.setattr(run, "FeedMarketServices", FakeFeed)
    monkeypatch.setattr(run, "CONTOURS_MARKET_FEED", market_feed)
    monkeypatch.setattr(run, "TradingContour", FakeContour)
    monkeypatch.setattr(run, "acquire_runtime_lock", lambda state_dir: None)
    monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(run, "MARKET_DATA_SETTINGS", SimpleNamespace(independent_lanes=True))
    monkeypatch.setattr(run, "_choose_instruments", lambda *a, **k: order.append("choose") or [("SBER", "SBER", "share", "SBER")])
    code, _, _ = run._launch(state_dir=tmp_path, client_provider=object(), clock=None, session=SimpleNamespace(),
                             channel_names=[])
    assert code == 0
    feed = ["feed"] if market_feed else []
    closing = ["feed.close", "core.close"] if market_feed else ["core.close"]
    assert order == ["early", "api", "choose", "data", "api", *feed, "build", "demand", "serve", "run:waiter",
                     *closing, "contour.close"]


@pytest.mark.parametrize("result, acknowledged", [(True, 1), (None, 1), (False, 0)])
def test_feed_cursor_is_acknowledged_only_after_committed_tick(monkeypatch, result, acknowledged):
    captured, acks = {}, []

    class Bot:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            captured["result"] = captured["post_tick"]({"1m"})

    monkeypatch.setattr(contour, "TradingBot", Bot)
    runtime = contour._Runtime(post_tick=lambda ready: result)
    contour._run_bot([], None, runtime, data_cache=None, timeline=None, after_tick=lambda: acks.append(1))
    assert captured["result"] is result and len(acks) == acknowledged
