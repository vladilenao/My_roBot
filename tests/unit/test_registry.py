import pandas as pd
import pytest
from typing import get_args

from src.strategies.contracts import Decision, SignalType
from src.strategies.registry import (
    _discover_strategies,
    all_strategies,
    get_strategy,
    register,
    strategy_names,
    validate_assignments,
)
from src.strategies.names import StrategyName


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr("src.strategies.registry._registry", {})


class DummyStrategy:
    NAME = "dummy_a"
    STRATEGY_WINDOW = 3

    def compute(self, df):
        return df

    def decide(self, ta):
        return Decision(SignalType.HOLD, 0.0)

    def expected_events(self, ta):
        return pd.DataFrame()

    def required_history(self):
        return self.STRATEGY_WINDOW + 10


def test_register_returns_class():
    assert register(DummyStrategy) is DummyStrategy


def test_get_strategy_returns_instance():
    register(DummyStrategy)
    strategy = get_strategy("dummy_a")
    assert strategy.NAME == "dummy_a"
    assert strategy.STRATEGY_WINDOW == 3


def test_unknown_name_raises_with_available_list():
    register(DummyStrategy)
    with pytest.raises(ValueError, match="dummy_a"):
        get_strategy("nonexistent")


def test_duplicate_name_raises():
    class AnotherDummy:
        NAME = "dummy_a"

    register(DummyStrategy)
    with pytest.raises(ValueError, match="уже зарегистрирована"):
        register(AnotherDummy)


def test_all_strategies_returns_instances():
    register(DummyStrategy)
    strategies = all_strategies()
    assert len(strategies) == 1
    assert isinstance(strategies[0], DummyStrategy)


def test_registry_starts_empty_in_isolation(monkeypatch):
    monkeypatch.setattr("src.strategies.registry._registry", {})
    from src.strategies.registry import _registry as fresh

    assert fresh == {}


def test_literal_names_match_registry():
    from src.strategies.registry import _registry as live
    from src.strategies.macd_rsi_stoch import MacdRsiStochStrategy

    register(MacdRsiStochStrategy)
    assert set(get_args(StrategyName)) == set(live)


def test_discover_strategies_imports_packages_once(monkeypatch):
    import types

    calls = []
    monkeypatch.setattr("src.strategies.registry._packages_loaded", False)

    mock_package = types.ModuleType("src.strategies")
    mock_package.__path__ = []

    def mock_import(name):
        calls.append(name)
        return mock_package

    monkeypatch.setattr("src.strategies.registry.importlib.import_module", mock_import)

    _discover_strategies()
    _discover_strategies()

    assert calls == ["src.strategies"]


def test_discover_strategies_skips_when_already_loaded(monkeypatch):
    import types

    calls = []
    monkeypatch.setattr("src.strategies.registry._packages_loaded", True)

    mock_package = types.ModuleType("src.strategies")
    mock_package.__path__ = []

    def mock_import(name):
        calls.append(name)
        return mock_package

    monkeypatch.setattr("src.strategies.registry.importlib.import_module", mock_import)

    _discover_strategies()

    assert calls == []


class AlphaDummy:
    NAME = "a_dummy"
    STRATEGY_WINDOW = 3

    def compute(self, df):
        return df

    def decide(self, ta):
        return Decision(SignalType.HOLD, 0.0)


def test_strategy_names_returns_sorted_keys():
    register(DummyStrategy)
    register(AlphaDummy)

    assert strategy_names() == ["a_dummy", "dummy_a"]


def test_validate_assignments_accepts_known_names():
    register(DummyStrategy)

    validate_assignments({"T1": ["dummy_a"], "T2": []})


def test_validate_assignments_lists_unknown_and_available():
    register(DummyStrategy)

    with pytest.raises(ValueError) as exc_info:
        validate_assignments({"T1": ["z_ghost", "a_ghost"], "T2": ["dummy_a"]})

    message = str(exc_info.value)
    assert "z_ghost" in message
    assert "a_ghost" in message
    assert "dummy_a" in message
    assert message.index("a_ghost") < message.index("z_ghost")


def test_validate_assignments_reports_source_dictionary():
    register(DummyStrategy)

    with pytest.raises(ValueError) as exc_info:
        validate_assignments({"NG": ["z_ghost"]}, source="FUTURE_STRATEGIES")

    message = str(exc_info.value)
    assert "FUTURE_STRATEGIES" in message
    assert "z_ghost" in message
    assert "dummy_a" in message


def test_validate_assignments_applies_to_both_dictionaries():
    register(DummyStrategy)

    validate_assignments({"SBER": ["dummy_a"]}, source="SHARE_STRATEGIES")

    with pytest.raises(ValueError) as exc_info:
        validate_assignments({"NG": ["a_ghost", "z_ghost"]}, source="FUTURE_STRATEGIES")

    message = str(exc_info.value)
    assert "FUTURE_STRATEGIES" in message
    assert "a_ghost" in message
    assert "z_ghost" in message


def test_import_names_module_is_light():
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        "import src.strategies.names;"
        "assert not any(m.startswith('src.strategies.macd_rsi_stoch') for m in sys.modules);"
        "assert not any('pandas_ta' in m for m in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
