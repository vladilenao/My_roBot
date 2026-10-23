"""Настройки контура эмуляции (add-contour-process, D9, D10)."""

import pytest

from src.config_loader import ConfigError, load_config


def _load(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text, encoding="utf-8")
    return load_config({}, config_file=path)


def test_channel_env_name(tmp_path):
    config = _load(tmp_path, '[contours.emulation]\ntelegram_channel_env = "TELEGRAM_EMULATION_CHANNEL_ID"\n')

    assert config["contours_emulation_telegram_channel_env"] == "TELEGRAM_EMULATION_CHANNEL_ID"


def test_feed_poll_still_accepted(tmp_path):
    assert _load(tmp_path, "[contours]\nfeed_poll_secs = 2\n")["contours_feed_poll_secs"] == 2


def test_removed_market_feed_switch(tmp_path):
    with pytest.raises(ConfigError, match="market_feed удалён"):
        _load(tmp_path, "[contours]\nmarket_feed = false\n")


def test_bad_env_name(tmp_path):
    with pytest.raises(ConfigError, match="telegram_channel_env"):
        _load(tmp_path, '[contours.emulation]\ntelegram_channel_env = "telegram-id"\n')


def test_unknown_contour_key(tmp_path):
    with pytest.raises(ConfigError, match=r"\[contours.emulation\] незнакомые ключи \['assignments'\]"):
        _load(tmp_path, '[contours.emulation]\nassignments = "all"\n')


def test_channel_id_comes_from_named_variable(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "CONTOUR_EMULATION_TELEGRAM_CHANNEL_ENV", "TELEGRAM_EMULATION_CHANNEL_ID")
    monkeypatch.setenv("TELEGRAM_EMULATION_CHANNEL_ID", "-100777")
    assert config.telegram_channel_id() == "-100777"
    monkeypatch.delenv("TELEGRAM_EMULATION_CHANNEL_ID")
    assert config.telegram_channel_id() is None


def test_missing_channel_disables_telegram_with_warning(monkeypatch, capsys):
    from src import config
    from src.notifier.factory import build_channels

    monkeypatch.setattr(config, "CONTOUR_EMULATION_TELEGRAM_CHANNEL_ENV", "TELEGRAM_EMULATION_CHANNEL_ID")
    monkeypatch.delenv("TELEGRAM_EMULATION_CHANNEL_ID", raising=False)

    channels = build_channels(["telegram"])

    assert "Telegram контура выключен: в .env не задан TELEGRAM_EMULATION_CHANNEL_ID" in capsys.readouterr().out
    assert not channels[0]._enabled
