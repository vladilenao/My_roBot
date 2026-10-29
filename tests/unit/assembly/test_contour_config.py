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


def test_sandbox_section_defaults():
    from src import config

    assert config._DEFAULTS["contours_sandbox_telegram_bot_env"] == "TELEGRAM_ACCOUNT_BOT_TOKEN"
    assert config._DEFAULTS["contours_sandbox_telegram_channel_env"] == "TELEGRAM_SANDBOX_CHANNEL_ID"
    assert config._DEFAULTS["contours_sandbox_reconcile_every_min"] == 1


def test_sandbox_section_values(tmp_path):
    config = _load(tmp_path, '[contours.sandbox]\ntelegram_channel_env = "SANDBOX_CHANNEL"\nreconcile_every_min = 5\n')
    assert config["contours_sandbox_telegram_channel_env"] == "SANDBOX_CHANNEL"
    assert config["contours_sandbox_reconcile_every_min"] == 5


def test_sandbox_bindings_rejected(tmp_path):
    with pytest.raises(ConfigError, match="PUT /api/v1/sandbox/target"):
        _load(tmp_path, '[contours.sandbox]\nbindings = ["future-ng-macd"]\n')


@pytest.mark.parametrize("text", ["reconcile_every_min = 0", "reconcile_every_min = 61", "telegram_bot_env = \"bot-token\"",
                                  "orders = \"yes\"", "exit_requote_attempts = 0", "exit_offset_steps = 101",
                                  "exit_requote_secs = 4", "unknown_key = 1"])
def test_sandbox_bad_values(tmp_path, text):
    with pytest.raises(ConfigError, match="contours.sandbox"):
        _load(tmp_path, f"[contours.sandbox]\n{text}\n")


def test_sandbox_orders_and_exit_settings(tmp_path):
    """add-tinvest-orders: режим заявок и перевыставление выходов; по умолчанию — заявки, 5 шагов, 15 с, 3 раза."""
    from src import config

    assert (config._DEFAULTS["contours_sandbox_orders"], config._DEFAULTS["contours_sandbox_exit_offset_steps"],
            config._DEFAULTS["contours_sandbox_exit_requote_secs"],
            config._DEFAULTS["contours_sandbox_exit_requote_attempts"]) == (True, 5, 15, 3)
    loaded = _load(tmp_path, "[contours.sandbox]\norders = false\nexit_offset_steps = 0\nexit_requote_attempts = 10\n")
    assert loaded["contours_sandbox_orders"] is False
    assert (loaded["contours_sandbox_exit_offset_steps"], loaded["contours_sandbox_exit_requote_attempts"]) == (0, 10)
