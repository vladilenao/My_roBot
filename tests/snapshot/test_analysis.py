import pytest

from tests.snapshot import helper


def _discover_cases():
    if not helper.DATA_DIR.exists():
        return []
    cases = set()
    for expected_path in helper.DATA_DIR.glob("*/*_expected_context.csv"):
        case = expected_path.parent.name
        mode = expected_path.name[: -len("_expected_context.csv")]
        cases.add((case, mode))
    return sorted(cases)


CASES = _discover_cases()


@pytest.mark.parametrize("case,mode", CASES)
def test_analysis_snapshot(case, mode, request):
    df = helper.load_candles_fixture(case)
    actual = helper.analysis_expected_events(mode, df)

    if request.config.getoption("--update-snapshots"):
        helper.write_analysis_expected(case, mode, actual)
        return

    try:
        expected = helper.load_analysis_expected(case, mode)
        helper.compare_analysis(actual, expected)
    except AssertionError as err:
        row, a, e = helper.first_analysis_divergence(actual, expected)
        pytest.fail(
            f"Кейс '{case}', режим '{mode}': эталон разошёлся с текущим поведением.\n"
            f"Первое расхождение - строка {row}:\n"
            f"  фактическое: {a}\n"
            f"  эталонное:   {e}\n"
            f"{err}"
        )
