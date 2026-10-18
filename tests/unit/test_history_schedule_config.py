import tomllib
from pathlib import Path

import pytest

from src.config_loader import HISTORY_SCHEDULE_DEFAULTS, ConfigError, load_config


def config(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text, encoding="utf-8")
    return load_config({"historical_schedule": dict(HISTORY_SCHEDULE_DEFAULTS)}, config_file=path)


def test_default_uses_reconstructed_schedule(tmp_path):
    assert config(tmp_path, "")["historical_schedule"] == {"use_reconstructed": True}


def test_reconstructed_schedule_can_be_disabled(tmp_path):
    result = config(tmp_path, "[historical.schedule]\nuse_reconstructed = false\n")

    assert result["historical_schedule"] == {"use_reconstructed": False}


@pytest.mark.parametrize("text, message", [
    ('[historical.schedule]\nuse_reconstructed = "yes"', "historical.schedule.use_reconstructed"),
    ("[historical.schedule]\nuse_reconstructed = 1", "historical.schedule.use_reconstructed"),
    ("[historical.schedule]\nunknown = true", "незнакомые ключи"),
])
def test_invalid_section_names_file_and_key(tmp_path, text, message):
    with pytest.raises(ConfigError) as error:
        config(tmp_path, text)

    assert "robot.toml" in str(error.value) and message in str(error.value)


def test_embedded_default_toml_has_section():
    data = tomllib.loads(Path("default.toml").read_text(encoding="utf-8"))

    assert data["historical"]["schedule"] == {"use_reconstructed": True}
