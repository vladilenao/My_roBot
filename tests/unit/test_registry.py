import pandas as pd
import pytest
from typing import get_args

from src.strategies import (
    _ensure_registered,
    all_strategies,
    get_strategy,
    register,
    strategy_names,
    validate_assignments,
)
from src.strategies.base import Decision, SignalType
from src.strategies.names import StrategyName


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr("src.strategies._registry", {})


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
    monkeypatch.setattr("src.strategies._registry", {})
    from src.strategies import _registry as fresh

    assert fresh == {}


def test_literal_names_match_registry():
    from src.strategies import _registry as live
    from src.strategies.macd_rsi_stoch import MacdRsiStochStrategy

    register(MacdRsiStochStrategy)
    assert set(get_args(StrategyName)) == set(live)


def test_ensure_registered_imports_packages_once(monkeypatch):
    calls = []
    monkeypatch.setattr("src.strategies._packages_loaded", False)
    monkeypatch.setattr("src.strategies._import_module", lambda name: calls.append(name))

    _ensure_registered()
    _ensure_registered()

    assert calls == ["src.strategies.macd_rsi_stoch"]


def test_ensure_registered_skips_when_already_loaded(monkeypatch):
    calls = []
    monkeypatch.setattr("src.strategies._packages_loaded", True)
    monkeypatch.setattr("src.strategies._import_module", lambda name: calls.append(name))

    _ensure_registered()

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
