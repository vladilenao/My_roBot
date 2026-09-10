import sys

import pytest

from src.config_loader import ConfigError, app_dir, load_config


def _defaults():
    return {
        "timeframe": "1h",
        "sleep_seconds": 3600,
        "heartbeat_every_ticks": 60,
        "tick_poll_secs": 1,
        "tick_timeout_secs": 65,
        "instrument_type": "future",
        "ticker": "NGU6",
        "notifier": "console",
        "share_strategies": {"SBER": ["macd_rsi_stoch"]},
        "future_strategies": {"NG": ["macd_rsi_stoch"], "ED": ["flat_triangle"]},
    }


def test_app_dir_resolves_to_project_root_in_dev():
    from pathlib import Path

    import src.config_loader as loader

    assert app_dir() == Path(loader.__file__).resolve().parent.parent


def test_app_dir_uses_executable_folder_when_frozen(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/fake/app/robot-v1")
    assert app_dir() == Path("/fake/app")


def test_external_config_overrides_bundled_defaults(tmp_path):
    bundled = tmp_path / "default.toml"
    bundled.write_text("[robot]\ntimeframe = \"1h\"\n", encoding="utf-8")
    external = tmp_path / "robot.toml"
    external.write_text("[robot]\ntimeframe = \"5m\"\n", encoding="utf-8")

    cfg = load_config(_defaults(), config_file=external, bundled_file=bundled)
    assert cfg["timeframe"] == "5m"


def test_bundled_default_used_when_no_external(tmp_path):
    bundled = tmp_path / "default.toml"
    bundled.write_text("[robot]\ntimeframe = \"5m\"\n", encoding="utf-8")
    missing = tmp_path / "no_such_robot.toml"

    cfg = load_config(_defaults(), config_file=missing, bundled_file=bundled)
    assert cfg["timeframe"] == "5m"


def test_missing_files_fall_back_to_defaults(tmp_path):
    missing = tmp_path / "no_such_file.toml"
    cfg = load_config(
        _defaults(), config_file=missing, bundled_file=missing
    )
    assert cfg == _defaults()


def test_unknown_key_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[robot]\nbogus = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_unknown_section_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[unknown]\nfoo = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_invalid_notifier_channel_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannel = \"sms\"\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_wrong_type_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[robot]\nsleep_seconds = \"fast\"\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)