import pytest

from src.strategies import get_strategy
from src.strategies.macd_rsi_stoch_strategy import DEFAULT_CONFIG as MACD_RSI_STOCH_CONFIG
from src.strategies.flat_triangle_strategy import DEFAULT_CONFIG as FLAT_TRIANGLE_CONFIG
from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG as HARMONIC_ABCD_CONFIG
from tests.snapshot import helper

STRATEGY_CONFIGS = {
    "macd_rsi_stoch": MACD_RSI_STOCH_CONFIG,
    "flat_triangle": FLAT_TRIANGLE_CONFIG,
    "harmonic_abcd": HARMONIC_ABCD_CONFIG,
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
