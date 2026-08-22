import pandas as pd
import pytest

from src.strategies import _registry, all_strategies, get_strategy, register
from src.strategies.base import Decision, SignalType


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
