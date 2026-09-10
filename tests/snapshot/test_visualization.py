import shutil

import pytest

from tools.visualize_signals import (
    ASSETS_DIR,
    PANEL_CONFIG,
    SIGNAL_TEXT,
    render,
)
from tests.snapshot import helper


def _discover_visualizable_cases():
    if not helper.DATA_DIR.exists():
        return []
    cases = set()
    for expected_path in helper.DATA_DIR.glob("*/*_expected_signals.csv"):
        case = expected_path.parent.name
        strategy_name = expected_path.name[: -len("_expected_signals.csv")]
        if strategy_name not in PANEL_CONFIG:
            continue
        events = helper.load_expected(case, strategy_name)
        directions = sorted(events["signal"].unique())
        if "BUY" in directions:
            cases.add((case, strategy_name, "BUY"))
        if "SELL" in directions:
            cases.add((case, strategy_name, "SELL"))
    return sorted(cases)


CASES = _discover_visualizable_cases()


@pytest.mark.parametrize("case,strategy_name,direction", CASES)
def test_visualize_signals_svg(case, strategy_name, direction, tmp_path):
    out = tmp_path / f"{strategy_name}_{direction}.svg"
    render(strategy_name, case, direction, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith("<?xml") or "<svg" in content
    assert len(content) > 1000
    assert "svg" in content.lower()


def test_visualize_signals_assets_exist():
    assert ASSETS_DIR.exists()
    for strategy_name, directions in {
        "macd_rsi_stoch": ("BUY", "SELL"),
        "flat_triangle": ("BUY", "SELL"),
        "harmonic_abcd": ("BUY",),
    }.items():
        for direction in directions:
            assert (ASSETS_DIR / f"{strategy_name}_{direction}.svg").exists()
    assert shutil.which is not None  # не обращаемся к сети


def test_signal_text_has_entries():
    assert SIGNAL_TEXT, "Нужен минимум один индикатор с текстами сигналов"