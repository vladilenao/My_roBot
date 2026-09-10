import re


def test_version_imports_and_is_semver():
    from src import __version__

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)