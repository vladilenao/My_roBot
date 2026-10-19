"""Фактический roster конфигурации и сохранённые потребности сопровождения."""

from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

import src.config as config
from src.data.ingestion_settings import MarketDataSettings
from src.decision.filters.triple_screen import TripleScreenParams, tf_hierarchy
from src.history.warmup import live_data_requirements
from src.strategies.contracts import Assignment


def test_shipped_roster_fits_live_data_limits(monkeypatch):
    # Инварианты поставляемого default.toml, а не снимок состава: смена
    # таймфреймов стратегий не должна требовать правки чисел в тесте.
    # Каждый ticker/family здесь один UID.
    with (Path(__file__).resolve().parents[2] / "default.toml").open("rb") as source:
        settings = tomllib.load(source)
    monkeypatch.setattr(config, "TIMEFRAME", settings["robot"]["timeframe"])
    monkeypatch.setattr("src.history.warmup.get_strategy",
                        lambda name, profile: SimpleNamespace(required_history=lambda: 1))
    params = TripleScreenParams.from_config(settings["strategies"]["filter"]["triple_screen"])
    profiles = settings["trade_management"]["profiles"]
    ladder = tuple(config.TIMEFRAMES)
    roster = set()
    instruments = []
    for kind in ("share", "future"):
        assignments = config._to_assignments(settings["strategies"][kind])
        for ticker, bindings in assignments.items():
            uid = f"{kind}:{ticker}"
            instruments.append(uid)
            requirements = live_data_requirements(bindings, {binding.strategy: None for binding in bindings},
                profiles, params, ladder)
            timeframes = set(requirements)
            # Минутки — основа исполнения и защиты любой сделки.
            assert "1m" in timeframes, uid
            assert timeframes <= set(ladder), uid
            for binding in bindings:
                assert binding.timeframe in timeframes, (uid, binding.id)
                if binding.filter_profile == "triple_screen":
                    higher = tf_hierarchy(binding.timeframe, params.multiplier, ladder)
                    assert set(higher) <= timeframes, (uid, binding.id)
            assert all(count > 0 for count in requirements.values()), uid
            roster.update((uid, tf) for tf in requirements)
    assert instruments
    assert len(instruments) == len(set(instruments))
    # Весь выбранный состав помещается в подписки одного потока.
    soft_limit = settings.get("market_data", {}).get(
        "stream_subscription_soft_limit", MarketDataSettings.stream_subscription_soft_limit)
    assert len(roster) <= soft_limit


def test_saved_management_timeframe_remains_after_entry_binding_removed(monkeypatch):
    monkeypatch.setattr("src.history.warmup.get_strategy",
                        lambda name, profile: SimpleNamespace(required_history=lambda: 80))
    binding = Assignment(id="entry", strategy="macd_rsi_stoch", management="levels_rr", timeframe="15m")
    saved = {"1h": 120}
    requirements = live_data_requirements([binding, binding], {binding.strategy: None}, {},
        TripleScreenParams(), tuple(config.TIMEFRAMES), saved_requirements=saved)
    assert set(requirements) == {"1m", "15m", "1h"}
    assert requirements["1h"] == 120
    assert saved == {"1h": 120}
    assert live_data_requirements([], {}, {}, TripleScreenParams(), tuple(config.TIMEFRAMES),
                                  saved_requirements=saved) == {"1m": 1, "1h": 120}


@pytest.mark.parametrize("saved", [{"bad": 1}, {"1h": 0}, {"1h": True}, {"1h": float("inf")}])
def test_invalid_saved_requirement_is_not_silently_omitted(saved):
    with pytest.raises(ValueError, match="сохранённое требование"):
        live_data_requirements([], {}, {}, TripleScreenParams(), tuple(config.TIMEFRAMES),
                               saved_requirements=saved)
