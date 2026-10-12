"""Фактический roster конфигурации и сохранённые потребности сопровождения."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

import src.config as config
from src.decision.filters.triple_screen import TripleScreenParams
from src.history.warmup import live_data_requirements
from src.strategies.contracts import Assignment


def test_shipped_all_selected_roster_has_81_unique_pairs(monkeypatch):
    # Это конфигурационный fixture, не исторические/торговые данные и не
    # текущий выбор интерактивного меню. Каждый ticker/family здесь один UID.
    with (Path(__file__).resolve().parents[2] / "default.toml").open("rb") as source:
        settings = tomllib.load(source)
    monkeypatch.setattr(config, "TIMEFRAME", settings["robot"]["timeframe"])
    monkeypatch.setattr("src.history.warmup.get_strategy",
                        lambda name, profile: SimpleNamespace(required_history=lambda: 1))
    params = TripleScreenParams.from_config(settings["strategies"]["filter"]["triple_screen"])
    profiles = settings["trade_management"]["profiles"]
    roster = set()
    work_pairs = set()
    instruments = []
    for kind in ("share", "future"):
        assignments = config._to_assignments(settings["strategies"][kind])
        for ticker, bindings in assignments.items():
            uid = f"{kind}:{ticker}"
            instruments.append(uid)
            requirements = live_data_requirements(bindings, {binding.strategy: None for binding in bindings},
                profiles, params, tuple(config.TIMEFRAMES))
            roster.update((uid, tf) for tf in requirements)
            work_pairs.update((uid, binding.timeframe) for binding in bindings)
    assert len(instruments) == 23
    assert len(work_pairs) == 46
    assert len(roster) == 81
    assert Counter(tf for _, tf in roster) == {
        "1m": 23, "5m": 2, "15m": 22, "30m": 1, "1h": 22, "4h": 2, "1d": 5, "1w": 4,
    }
    assert {tf for uid, tf in roster if uid == "future:SI"} == {"1m", "5m", "30m", "4h"}
    # Арифметика ниже явно условна: один RPC/пара каждые 300 с и один
    # однодневный 1m чанк без overlap/retries, не реальная доступная квота.
    assert len(roster) * 60 / 300 == pytest.approx(16.2)
    assert len(instruments) * 30 == 690


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
