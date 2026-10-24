"""Раздельная сборка ядра и торгового контура (refactor-core-contour-split)."""

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import run
from src.assembly import contour, contracts, core
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
    # Модуль контура не импортируется; имена про «контур» (процесс, потребности) — допустимы.
    assert not {"src.assembly.contour", "contour"} & imported, sorted(imported)


def test_feed_services_do_not_import_core_or_history():
    imported = _imports(ROOT / "src" / "assembly" / "feed.py")
    assert not {"src.assembly.core", "src.assembly.history", "src.assembly.contour"} & imported, sorted(imported)


def test_contour_reaches_data_only_through_services_port():
    imported = _imports(ROOT / "src" / "assembly" / "contour.py")
    assert "src.assembly.core" not in imported and "src.assembly.history" not in imported


class TestBuildDemand:
    def test_notify_only_has_no_obligations(self, monkeypatch):
        monkeypatch.setattr(contracts, "SHARE_STRATEGIES", {"SBER": [SimpleNamespace(timeframe="1h")]})
        demand = contour.build_demand(contour._Runtime(), [SBER], data_cache=None)
        assert demand == ContourDemand((SBER,), pattern_pairs=((SBER, "1h"),))

    def test_pattern_pairs_are_assignment_timeframes_of_selected_instruments(self, monkeypatch):
        monkeypatch.setattr(contracts, "SHARE_STRATEGIES", {"SBER": [SimpleNamespace(timeframe="4h"),
                                                                   SimpleNamespace(timeframe="1h"),
                                                                   SimpleNamespace(timeframe="1h")]})
        monkeypatch.setattr(contracts, "FUTURE_STRATEGIES", {"NG": [SimpleNamespace(timeframe="15m")]})
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


def test_emulation_launch_runs_contour_process_after_core(monkeypatch, tmp_path):
    """Эмуляция (add-contour-process): ранний сбор и API до выбора, затем процесс контура, затем остановка ядра."""
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

        def run_contour(self, instruments, *, kind, control=None, source="", command="", core_state=None,
                        next_launch=None, resolve=None, recovery=None):
            assert kind == "emulation" and [item.ticker for item in instruments] == ["SBER"]
            # Желаемое ядра ведётся и без ключа управления; без API следующей команды нет.
            assert core_state is not None and core_state.path == tmp_path / "core.sqlite3"
            assert next_launch() is None
            order.append("run_contour")
            return 0

        def close(self):
            order.append("core.close")

    def no_contour(*args, **kwargs):
        raise AssertionError("в эмуляции торговый контур не собирается в процессе ядра")

    monkeypatch.setattr(run, "LiveCore", FakeCore)
    monkeypatch.setattr(run, "TradingContour", no_contour)
    monkeypatch.setattr(run, "acquire_runtime_lock", lambda state_dir: None)
    monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(run, "MARKET_DATA_SETTINGS", SimpleNamespace(independent_lanes=True))
    monkeypatch.setattr(run, "_choose_instruments", lambda *a, **k: order.append("choose") or [("SBER", "SBER", "share", "SBER")])
    code, _, _ = run._launch(state_dir=tmp_path, client_provider=object(), clock=None, session=SimpleNamespace(),
                             channel_names=[])
    assert code == 0
    assert order == ["early", "api", "choose", "data", "api", "run_contour", "core.close"]


def test_journal_migration_failure_stops_launch(monkeypatch, tmp_path, capsys):
    from src.assembly.process import JournalMigrationError

    class FakeCore:
        def __init__(self, state_dir, client_provider):
            self.early = None
            self.export_server = None

        def start_api(self, control=None):
            pass

        def ensure_market_data(self):
            return self

        def run_contour(self, instruments, **kwargs):
            raise JournalMigrationError("Журнал эмуляции не перенесён")

        def close(self):
            pass

    monkeypatch.setattr(run, "LiveCore", FakeCore)
    monkeypatch.setattr(run, "acquire_runtime_lock", lambda state_dir: None)
    monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(run, "MARKET_DATA_SETTINGS", SimpleNamespace(independent_lanes=False))
    monkeypatch.setattr(run, "_choose_instruments", lambda *a, **k: [("SBER", "SBER", "share", "SBER")])
    code, _, _ = run._launch(state_dir=tmp_path, client_provider=object(), clock=None, session=SimpleNamespace(),
                             channel_names=[])
    assert code == 2
    assert "Журнал эмуляции не перенесён" in capsys.readouterr().out


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


def test_control_hooks_wrap_execution_in_tick(monkeypatch):
    order = []

    class Bot:
        def __init__(self, **kwargs):
            self.post_tick = kwargs["post_tick"]

        def run(self):
            order.append("run")
            self.post_tick({"1m"})

    class Control:
        def start(self, manager, roster=None):
            order.append(f"start:{manager}")

        def before_tick(self):
            order.append("before")

        def after_tick(self):
            order.append("after")

        def close(self):
            order.append("close")

    monkeypatch.setattr(contour, "TradingBot", Bot)
    runtime = contour._Runtime(post_tick=lambda ready: order.append("execute") or True, trade_manager="tm")
    contour._run_bot([], None, runtime, data_cache=None, timeline=None, after_tick=lambda: order.append("ack"),
                     control=Control())
    assert order == ["start:tm", "run", "before", "execute", "after", "ack", "close"]


def test_control_hook_runs_without_financial_part(monkeypatch):
    seen = {}

    class Bot:
        def __init__(self, **kwargs):
            seen["post_tick"] = kwargs["post_tick"]

        def run(self):
            seen["result"] = seen["post_tick"]({"1m"})

    control = SimpleNamespace(start=lambda manager, roster=None: None, before_tick=lambda: None, after_tick=lambda: None,
                              close=lambda: None)
    monkeypatch.setattr(contour, "TradingBot", Bot)
    contour._run_bot([], None, contour._Runtime(), data_cache=None, timeline=None, control=control)
    assert seen["result"] is True


def test_roster_admits_and_retires_instrument_everywhere():
    from src.history.contract_lifecycle import ConstantContractLifecycle

    calls, cards = [], []
    live = SimpleNamespace(register_needs_for=lambda item: calls.append(("needs", item.ticker)),
                           bootstrap_instrument=lambda item: calls.append(("overview", item.ticker)),
                           forget_instrument=lambda item: calls.append(("forget", item.ticker)))
    bot = SimpleNamespace(_instruments=[SBER], _live_analysis=live, _protection_timeframe="1m",
                          _strategies_for=lambda item: [SimpleNamespace(timeframe="1h")])
    lifecycle = ConstantContractLifecycle([SBER])
    runtime = contour._Runtime(storage=object(), contract_lifecycle=lifecycle)
    runtime.instruments = [SBER]
    runtime.execution_instruments = [SBER]
    runtime.on_contracts = cards.append
    statuses = {("NGZ6", "1m"): "READY", ("NGZ6", "1h"): "BOOTSTRAPPING"}
    cache = SimpleNamespace(pair_status=lambda item, tf: {"state": statuses[item.ticker, tf]})
    forgotten = []
    roster = contour._Roster(bot, runtime, cache, forget=lambda item: forgotten.append(item.ticker))

    assert roster.warm(NG) and not roster.ready(NG)
    statuses["NGZ6", "1h"] = "READY"
    assert roster.ready(NG)
    roster.admit(NG, {"uid-ng": "card"})
    assert calls == [("needs", "NGZ6"), ("overview", "NGZ6")] and cards == [{"uid-ng": "card"}]
    assert [item.ticker for item in bot._instruments] == ["SBER", "NGZ6"]
    assert [item.ticker for item in lifecycle.instruments] == ["SBER", "NGZ6"]
    assert [item.ticker for item in runtime.instruments] == ["SBER", "NGZ6"]
    assert runtime.execution_aliases == {"uid-ng": "uid-ng", "NGZ6": "uid-ng"}
    assert roster.keys() == {"uid-sber", "uid-ng"} and roster.instrument("uid-ng") is NG

    roster.retire(NG)
    assert [item.ticker for item in bot._instruments] == ["SBER"] and lifecycle.instruments == [SBER]
    assert runtime.execution_instruments == [SBER] and runtime.execution_aliases == {}
    assert calls[-1] == ("forget", "NGZ6") and forgotten == ["NGZ6"]
