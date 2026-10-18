from datetime import datetime

import pytest

from src.config_loader import HISTORY_LOGGING_DEFAULTS, ConfigError, load_config


def config(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text, encoding="utf-8")
    return load_config({"historical_logging": dict(HISTORY_LOGGING_DEFAULTS)}, config_file=path)


def test_defaults_are_info_without_rotation(tmp_path):
    assert config(tmp_path, "")["historical_logging"] == {
        "level": "INFO", "max_bytes": 0, "backup_count": 0, "debug_from": None, "debug_to": None,
    }


def test_partial_table_keeps_defaults_and_normalizes_level(tmp_path):
    result = config(tmp_path, '[historical.logging]\nlevel = "debug"\n')["historical_logging"]

    assert result["level"] == "DEBUG"
    assert result["max_bytes"] == 0


def test_debug_window_without_zone_is_utc_and_with_zone_is_converted(tmp_path):
    result = config(tmp_path, (
        '[historical.logging]\n'
        'debug_from = "2025-08-07 14:00+03:00"\n'
        'debug_to = "2025-08-07 18:00"\n'
    ))["historical_logging"]

    assert result["debug_from"] == datetime(2025, 8, 7, 11, 0)
    assert result["debug_to"] == datetime(2025, 8, 7, 18, 0)


def test_native_toml_datetime_is_accepted(tmp_path):
    result = config(tmp_path, (
        "[historical.logging]\ndebug_from = 2025-08-07T11:00:00\ndebug_to = 2025-08-07T15:00:00\n"
    ))["historical_logging"]

    assert (result["debug_from"], result["debug_to"]) == (datetime(2025, 8, 7, 11), datetime(2025, 8, 7, 15))


def test_empty_strings_mean_no_window(tmp_path):
    result = config(tmp_path, '[historical.logging]\ndebug_from = ""\ndebug_to = ""\n')["historical_logging"]

    assert result["debug_from"] is None and result["debug_to"] is None


@pytest.mark.parametrize("text", [
    '[historical.logging]\nlevel = "LOUD"',
    '[historical.logging]\nunknown = 1',
    '[historical.logging]\nmax_bytes = -1',
    '[historical.logging]\nbackup_count = 1.5',
    '[historical.logging]\nmax_bytes = true',
    '[historical.logging]\ndebug_from = "вчера"\ndebug_to = "2025-08-07 18:00"',
    '[historical.logging]\ndebug_from = "2025-08-07 14:00"',
    '[historical.logging]\ndebug_from = "2025-08-07 18:00"\ndebug_to = "2025-08-07 14:00"',
    '[historical.logging]\ndebug_from = 5\ndebug_to = 6',
])
def test_invalid_settings_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError, match=r"historical\.logging"):
        config(tmp_path, text)
