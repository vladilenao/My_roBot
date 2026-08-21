import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Перезаписать эталонные файлы expected_signals.csv текущим поведением конвейера вместо сверки.",
    )
