"""Ключ привязки clusters и секция [clusters] (change add-cluster-filter)."""

import pytest

from src.clusters.settings import ClusterSettings
from src.config_loader import ConfigError, load_config

ASSIGNMENT = '{ id = "future-br-macd", name = "macd_rsi_stoch", management = "levels_rr"%s }'


def _load(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text, encoding="utf-8")
    return load_config({}, config_file=path)


def _strategies(extra=""):
    return "[strategies.future.BR]\nstrategies = [\n    " + ASSIGNMENT % extra + ",\n]\n"


def test_defaults_without_section(tmp_path):
    config = _load(tmp_path, _strategies(', clusters = "shadow"'))

    settings = ClusterSettings.from_config(config.get("clusters_params") or {})
    assert config["future_strategies"]["BR"]["strategies"][0]["clusters"] == "shadow"
    assert (settings.pre_move_veto_sigma, settings.initiative_max_sigma, settings.extreme_position) == (1.5, 0.5, 0.8)
    assert (settings.climax_window_min, settings.initiative_window_min, settings.sigma_days) == (30, 60, 10)
    assert (settings.stream, settings.consumer_group, settings.enforce_level) == ("cluster.events", "robot-v1", "veto")


def test_assignment_without_key_is_off(tmp_path):
    from src.config import _to_assignments

    config = _load(tmp_path, _strategies())

    assert _to_assignments(config["future_strategies"])["BR"][0].clusters == "off"


def test_invalid_mode(tmp_path):
    with pytest.raises(ConfigError, match="future-br-macd.*off.*shadow.*enforce"):
        _load(tmp_path, _strategies(', clusters = "on"'))


def test_section_overrides(tmp_path):
    config = _load(tmp_path, "[clusters]\npre_move_veto_sigma = 2.0\nenforce_level = \"veto_strong\"\n")

    settings = ClusterSettings.from_config(config["clusters_params"])
    assert settings.pre_move_veto_sigma == 2.0
    assert settings.enforce_level == "veto_strong"


def test_password_key_is_rejected_without_value(tmp_path):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, '[clusters]\nredis_password = "s3cr3t-value"\n')

    assert "redis_password" in str(error.value)
    assert "s3cr3t-value" not in str(error.value)


@pytest.mark.parametrize("text", [
    "climax_window_min = 90\ninitiative_window_min = 60\n",
    "initiative_max_sigma = 2.0\n",
    "extreme_position = 1.5\n",
    "sigma_days = 0\n",
    'enforce_level = "strong"\n',
    'consumer_group = "Robot V1"\n',
])
def test_constraints(tmp_path, text):
    with pytest.raises(ConfigError, match=r"\[clusters\]"):
        _load(tmp_path, "[clusters]\n" + text)
