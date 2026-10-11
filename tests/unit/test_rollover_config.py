import pytest

from src.config_loader import ConfigError, load_config


def config(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text)
    return load_config({"contract_expiry_block_days": 2}, config_file=path)


def test_calendar_defaults_and_overrides(tmp_path):
    result = config(tmp_path, '[historical.rollover.families.BR]\nlead_days=3\n')
    assert result["historical_rollover"] == {"policy": "calendar", "families": {"BR": {"lead_days": 3}}}
    assert config(tmp_path, "")["contract_expiry_block_days"] == 2
    assert config(tmp_path, '[historical.rollover]\n')["historical_rollover"]["policy"] == "calendar"


@pytest.mark.parametrize("text", [
    '[historical.rollover]\npolicy="volume"',
    '[historical.rollover]\nunknown=1',
    '[historical.rollover.families.NG]\nlead_days=1',
    '[historical.rollover.families.NG]\nlead_days=-1',
    '[historical.rollover.families.NG]\nlead_days=2.5',
    '[historical.rollover.families.NG]\nlead_days=true',
    '[historical.rollover.families.UNKNOWN]\nlead_days=3',
    '[historical.rollover.families.NG]\nlead_days=3\n[historical.rollover.families.ng]\nlead_days=3',
    '[historical.rollover.families.NG]\nunexpected=3',
])
def test_invalid_rollover_settings(tmp_path, text):
    with pytest.raises(ConfigError, match="robot.toml"):
        config(tmp_path, text)
