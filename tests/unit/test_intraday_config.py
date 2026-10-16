"""Секция [trading.intraday]: дефолты, строгие ключи и ограничения."""

import pytest

from src.config_loader import ConfigError, app_dir, load_config
from src.trade_management.intraday_settings import IntradaySettings


def _write(tmp_path, body: str):
    cfg = tmp_path / "robot.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _load(tmp_path, body: str):
    return load_config({}, config_file=_write(tmp_path, f"[trading.intraday]\n{body}\n"))


def test_defaults_without_section():
    settings = IntradaySettings.from_config({})
    assert settings.enabled is True
    assert settings.max_timeframe == "15m"
    assert settings.entry_sessions == ("morning", "main")
    assert settings.no_entry_before_close_min == 60
    assert settings.flatten_before_close_min == 20
    assert settings.keep_if_tp1_and_breakeven is True


def test_bundled_default_toml_matches_code_defaults():
    cfg = load_config({}, bundled_file=app_dir() / "default.toml", config_file=app_dir() / "missing.toml")
    assert IntradaySettings.from_config(cfg["intraday"]) == IntradaySettings()


def test_partial_section_is_filled_with_defaults(tmp_path):
    cfg = _load(tmp_path, "no_entry_before_close_min = 90")
    assert cfg["intraday"] == {**IntradaySettings().as_config(), "no_entry_before_close_min": 90}


def test_empty_session_list_is_allowed(tmp_path):
    cfg = _load(tmp_path, "entry_sessions = []")
    assert IntradaySettings.from_config(cfg["intraday"]).raw_types == frozenset()


@pytest.mark.parametrize("body,key", [
    ('entry_sessions = ["main", "night"]', "entry_sessions"),
    ('entry_sessions = ["main", "main"]', "entry_sessions"),
    ('entry_sessions = "main"', "entry_sessions"),
    ('max_timeframe = "7m"', "max_timeframe"),
    ("no_entry_before_close_min = -1", "no_entry_before_close_min"),
    ("no_entry_before_close_min = 1.5", "no_entry_before_close_min"),
    ("no_entry_before_close_min = 10\nflatten_before_close_min = 20", "flatten_before_close_min"),
    ('enabled = "yes"', "enabled"),
    ('horizon = "intraday"', "horizon"),
])
def test_invalid_values_raise_config_error_with_key(tmp_path, body, key):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, body)
    assert "trading.intraday" in str(error.value)
    assert key in str(error.value)
    assert "robot.toml" in str(error.value)


@pytest.mark.parametrize("timeframe,expected", [
    ("1m", True), ("5m", True), ("15m", True), ("30m", False), ("1h", False), ("", False), (None, False), ("bogus", False),
])
def test_intraday_classification_by_timeframe_duration(timeframe, expected):
    assert IntradaySettings().is_intraday(timeframe) is expected
