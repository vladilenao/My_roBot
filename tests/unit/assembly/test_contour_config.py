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


def test_sandbox_stop_place_secs(tmp_path):
    """add-broker-protection: срок постановки стоп-заявки у брокера — 30 с по умолчанию, 5–300."""
    import pytest

    from src import config
    from src.config_loader import ConfigError

    assert config._DEFAULTS["contours_sandbox_stop_place_secs"] == 30
    assert _load(tmp_path, "[contours.sandbox]\nstop_place_secs = 300\n")["contours_sandbox_stop_place_secs"] == 300
    for bad in ("stop_place_secs = 2", "stop_place_secs = 301", 'stop_place_secs = "30"'):
        with pytest.raises(ConfigError, match="stop_place_secs"):
            _load(tmp_path, f"[contours.sandbox]\n{bad}\n")


def test_sandbox_guard_settings(tmp_path):
    """add-account-guards: дневной убыток 3% капитала и три отказа подряд по умолчанию."""
    import pytest

    from src import config
    from src.config_loader import ConfigError

    assert (config._DEFAULTS["contours_sandbox_max_daily_loss_pct"],
            config._DEFAULTS["contours_sandbox_max_rejects_in_row"]) == (3.0, 3)
    loaded = _load(tmp_path, "[contours.sandbox]\nmax_daily_loss_pct = 0.5\nmax_rejects_in_row = 1\n")
    assert (loaded["contours_sandbox_max_daily_loss_pct"], loaded["contours_sandbox_max_rejects_in_row"]) == (0.5, 1)
    assert _load(tmp_path, "[contours.sandbox]\nmax_daily_loss_pct = 2\n")["contours_sandbox_max_daily_loss_pct"] == 2
    for bad in ("max_daily_loss_pct = 60", "max_daily_loss_pct = -1", "max_rejects_in_row = 0",
                "max_rejects_in_row = 21", 'max_rejects_in_row = "3"', 'max_daily_loss_pct = "3"'):
        with pytest.raises(ConfigError, match="max_"):
            _load(tmp_path, f"[contours.sandbox]\n{bad}\n")


# --- контур «Счёт» (add-account-contour) ---------------------------------------------

def test_account_section_defaults():
    from src import config

    assert config._DEFAULTS["contours_account_telegram_bot_env"] == "TELEGRAM_ACCOUNT_BOT_TOKEN"
    assert config._DEFAULTS["contours_account_telegram_channel_env"] == "TELEGRAM_ACCOUNT_CHANNEL_ID"
    assert config._DEFAULTS["contours_account_reconcile_every_min"] == 1
    assert config._DEFAULTS["contours_account_orders"] is False


def test_account_section_values(tmp_path):
    config = _load(tmp_path, '[contours.account]\ntelegram_channel_env = "ACCOUNT_CHANNEL"\nreconcile_every_min = 3\n'
                             'orders = false\n')
    assert config["contours_account_telegram_channel_env"] == "ACCOUNT_CHANNEL"
    assert config["contours_account_reconcile_every_min"] == 3 and config["contours_account_orders"] is False


@pytest.mark.parametrize("text, match", [
    ("max_rejects_in_row = 0", "от 1 до 20"),
    ("max_daily_loss_pct = 60", "от 0 до 50"),
    ('account_id = "2000123456"', "незнакомые ключи"),
    ('bindings = ["future-ng-macd"]', "командами API с Р2"),
    ("reconcile_every_min = 0", "от 1 до 60"),
    ('telegram_bot_env = "bad name"', "имя переменной окружения"),
])
def test_account_section_rejected(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, f"[contours.account]\n{text}\n")


def test_account_orders_enabled_with_trading_keys(tmp_path):
    """enable-live-trading: заявки на счёте включаются строкой в robot.toml, пороги — свои."""
    config = _load(tmp_path, "[contours.account]\norders = true\nmax_daily_loss_pct = 2\nstop_place_secs = 20\n")
    assert config["contours_account_orders"] is True
    assert config["contours_account_max_daily_loss_pct"] == 2 and config["contours_account_stop_place_secs"] == 20


# --- свой риск портфеля контура (add-contour-risk-limit) --------------------------------

@pytest.mark.parametrize("kind", ["emulation", "sandbox", "account"])
def test_contour_portfolio_pct_loaded(tmp_path, kind):
    config = _load(tmp_path, f"[contours.{kind}]\nportfolio_pct = 6\n")
    assert config[f"contours_{kind}_portfolio_pct"] == 6


@pytest.mark.parametrize("value, match", [("150", r"\[contours.account\] portfolio_pct должен быть в \[0, 100\]"),
                                          ("-1", "должен быть >= 0"), ("true", "конечным числом"),
                                          ('"5"', "")])
def test_contour_portfolio_pct_rejected(tmp_path, value, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, f"[contours.account]\nportfolio_pct = {value}\n")


def test_contour_portfolio_pct_below_min_trade_risk_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"min_trade_risk_pct \(1.0\) не может превышать \[contours.sandbox\]"):
        _load(tmp_path, "[trading.risk_limits]\nmin_trade_risk_pct = 1.0\n[contours.sandbox]\nportfolio_pct = 0.5\n")


def test_contour_risk_limits_use_own_percent():
    from decimal import Decimal

    from src.assembly.contour import RISK_LIMITS, _risk_limits
    from src.trade_management.models import PortfolioBudgetPolicy

    assert _risk_limits(6).portfolio == Decimal("6")
    assert _risk_limits().portfolio == Decimal(str(RISK_LIMITS.get("portfolio_pct", 2)))
    budget = PortfolioBudgetPolicy(Decimal("6")).budget(Decimal("9900"), Decimal("9900"))
    assert budget == Decimal("594")             # хватает на лот Si с риском 528 ₽, при 2% было 198 ₽


def test_contour_portfolio_pct_by_kind(monkeypatch):
    import run
    from src import config

    monkeypatch.setattr(config, "CONTOUR_ACCOUNT_PORTFOLIO_PCT", 6)
    monkeypatch.setattr(config, "CONTOUR_SANDBOX_PORTFOLIO_PCT", None)
    assert run._contour_portfolio_pct("account") == 6
    assert run._contour_portfolio_pct("sandbox") is None
    assert run._contour_portfolio_pct("history") is None
