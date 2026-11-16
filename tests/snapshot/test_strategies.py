import pytest

from src.config import ANALYSIS_DEPTH
from src.strategies import get_strategy
from src.strategies.macd_rsi_stoch_strategy import (
    DEFAULT_CONFIG as MACD_RSI_STOCH_CONFIG,
)
from src.strategies.flat_triangle_strategy import DEFAULT_CONFIG as FLAT_TRIANGLE_CONFIG
from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG as HARMONIC_ABCD_CONFIG
from src.strategies.ma_cloud_rsi_macd_strategy import (
    DEFAULT_CONFIG as MA_CLOUD_RSI_MACD_CONFIG,
)
from tests.snapshot import helper

STRATEGY_CONFIGS = {
    "macd_rsi_stoch": MACD_RSI_STOCH_CONFIG,
    "flat_triangle": FLAT_TRIANGLE_CONFIG,
    "harmonic_abcd": HARMONIC_ABCD_CONFIG,
    "ma_cloud_rsi_macd": MA_CLOUD_RSI_MACD_CONFIG,
}


def _discover_cases():
    if not helper.DATA_DIR.exists():
        return []
    cases = set()
    for expected_path in helper.DATA_DIR.glob("*/*_expected_signals.csv"):
        case = expected_path.parent.name
        strategy_name = expected_path.name[: -len("_expected_signals.csv")]
        cases.add((case, strategy_name))
    return sorted(cases)


CASES = _discover_cases()


@pytest.mark.parametrize("case,strategy_name", CASES)
def test_strategy_snapshot(case, strategy_name, request):
    df = helper.load_candles_fixture(case)
    config = STRATEGY_CONFIGS[strategy_name]
    strategy = get_strategy(strategy_name, config=config)
    ta = strategy.compute(df)
    actual = helper.expected_events(strategy_name, ta, config)

    if request.config.getoption("--update-snapshots"):
        helper.write_expected(case, strategy_name, actual)
        return

    try:
        expected = helper.load_expected(case, strategy_name)
        helper.compare_events(actual, expected)
    except AssertionError as err:
        row, a, e = helper.first_divergence(actual, expected)
        pytest.fail(
            f"Кейс '{case}', стратегия '{strategy_name}': эталон разошёлся с текущим поведением.\n"
            f"Первое расхождение - строка {row}:\n"
            f"  фактическое: {a}\n"
            f"  эталонное:   {e}\n"
            f"{err}"
        )


@pytest.mark.parametrize("case,strategy_name", CASES)
def test_analysis_depth_window_decides_like_full_series(case, strategy_name):
    """Окно глубины расчёта решает так же, как весь ряд: живой контур = история (add-analysis-depth)."""
    df = helper.load_candles_fixture(case)
    if len(df) <= ANALYSIS_DEPTH + 50:
        pytest.skip(f"фикстура {case} короче глубины расчёта с запасом")
    config = STRATEGY_CONFIGS[strategy_name]
    full, windowed = get_strategy(strategy_name, config=config), get_strategy(strategy_name, config=config)
    ta = full.compute(df)
    for end in range(ANALYSIS_DEPTH + 50, len(df) + 1):
        window = df.iloc[end - ANALYSIS_DEPTH:end].reset_index(drop=True)
        expected = full.decide(ta.iloc[:end]).signal_type
        actual = windowed.decide(windowed.compute(window)).signal_type
        assert actual == expected, (
            f"Кейс '{case}', стратегия '{strategy_name}', бар {end - 1} ({df['datetime'].iloc[end - 1]}): "
            f"окно {ANALYSIS_DEPTH} даёт {actual.value}, весь ряд — {expected.value}"
        )
