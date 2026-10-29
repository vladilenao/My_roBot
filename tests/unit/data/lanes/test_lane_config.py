"""Ключи [market_data] независимых полос: приём, валидация, устаревшие параметры (8.1)."""

import pytest

from src.config_loader import ConfigError, load_config
from src.data.ingestion_settings import MarketDataSettings


def _defaults():
    return {"timeframe": "1h", "instrument_type": "future", "ticker": "NGU6"}


def _settings(tmp_path, body):
    path = tmp_path / "robot.toml"
    path.write_text("[market_data]\n" + body, encoding="utf-8")
    config = load_config(_defaults(), config_file=path, bundled_file=tmp_path / "missing.toml")
    return MarketDataSettings.from_flat(config)


def test_lane_keys_are_read_from_toml(tmp_path):
    settings = _settings(tmp_path, """
independent_lanes = true
unary_requests_per_minute = 400
instrument_requests_per_minute = 100
quote_lane_shares = {live = 40, evidence = 30, completeness = 30}
instrument_lane_shares = {interactive = 70, calendar = 30}
evidence_window_secs = 1800
evidence_retry_secs = [30, 120]
completeness_recheck_secs = [600, 7200, 86400]
completeness_rescan_interval_secs = 900
lane_status_log_interval_secs = 60
""")
    assert settings.independent_lanes is True
    assert settings.lane_shares() == {"quotes": {"live": 40, "evidence": 30, "completeness": 30},
                                      "instruments": {"interactive": 70, "calendar": 30}}
    assert settings.evidence_retry_secs == (30, 120)
    assert settings.completeness_recheck_secs == (600, 7200, 86400)
    assert settings.deprecated_overrides() == ()


@pytest.mark.parametrize("body", [
    "quote_lane_shares = {live = 50, evidence = 25, completeness = 30}\n",
    "quote_lane_shares = {live = 100}\n",
    "completeness_recheck_secs = [3600, 600]\n",
    "evidence_retry_secs = []\n",
])
def test_invalid_lane_keys_are_rejected(tmp_path, body):
    with pytest.raises(ConfigError):
        _settings(tmp_path, body)


def test_old_config_is_accepted_and_round_keys_are_reported_as_deprecated(tmp_path):
    old = _settings(tmp_path, "recovery_round_timeout_secs = 900\n")
    assert old.independent_lanes is True                                    # полосы — режим по умолчанию
    assert old.deprecated_overrides() == ("recovery_round_timeout_secs",)
    legacy = _settings(tmp_path, "independent_lanes = false\nrecovery_round_timeout_secs = 900\n")
    assert legacy.deprecated_overrides() == ()  # прежняя очередь: параметр действует


def test_default_profile_is_not_reported_as_deprecated(tmp_path):
    defaults = {**_defaults(), **{f"market_data_{name}": value
                                  for name, value in MarketDataSettings(independent_lanes=True).as_dict().items()}}
    path = tmp_path / "robot.toml"
    path.write_text("[market_data]\nlive_transport = \"stream\"\n", encoding="utf-8")
    config = load_config(defaults, config_file=path, bundled_file=tmp_path / "missing.toml")
    assert MarketDataSettings.from_flat(config).deprecated_overrides() == ()
