import pytest

from src.config import (
    CLEARING_TIMES,
    INITIAL_DEPOSIT,
    JOURNAL_FILE,
    MAX_RISK_PCT,
    trading_enabled,
)
from src.config_loader import ConfigError, load_config


def _defaults():
    return {
        "timeframe": "1h",
        "sleep_seconds": 3600,
        "instrument_type": "future",
        "ticker": "NGU6",
        "notifier": "console",
    }


def _write(tmp_path, body: str):
    cfg = tmp_path / "robot.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


class TestTradingSectionParsing:
    def test_full_section_parses(self, tmp_path):
        cfg = _write(
            tmp_path,
            "[trading]\n"
            'initial_deposit = 50000\n'
            'max_risk_pct = 1.5\n'
            'journal_file = "journal.csv"\n'
            'clearing_times = ["14:05", "19:00"]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["initial_deposit"] == 50000
        assert result["max_risk_pct"] == 1.5
        assert result["journal_file"] == "journal.csv"
        assert result["clearing_times"] == ["14:05", "19:00"]

    def test_unknown_key_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\ndeposit = 50000\n")

        with pytest.raises(ConfigError, match="deposit"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_unknown_section_still_rejected(self, tmp_path):
        _write(tmp_path, "[trade]\ninitial_deposit = 50000\n")

        with pytest.raises(ConfigError, match="trade"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_no_trading_section_means_no_trading_keys(self, tmp_path):
        _write(tmp_path, "[robot]\ntimeframe = \"5m\"\n")

        result = load_config(_defaults(), config_file=cfg_ref(tmp_path))

        assert "initial_deposit" not in result
        assert "max_risk_pct" not in result


class TestTradingSectionValidation:
    def test_zero_deposit_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\ninitial_deposit = 0\n")
        with pytest.raises(ConfigError, match="initial_deposit"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_negative_deposit_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\ninitial_deposit = -100\n")
        with pytest.raises(ConfigError, match="initial_deposit"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_bool_deposit_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\ninitial_deposit = true\n")
        with pytest.raises(ConfigError, match="initial_deposit"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_zero_risk_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nmax_risk_pct = 0.0\n")
        with pytest.raises(ConfigError, match="max_risk_pct"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_over_100_risk_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nmax_risk_pct = 150.0\n")
        with pytest.raises(ConfigError, match="max_risk_pct"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_negative_risk_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nmax_risk_pct = -2.0\n")
        with pytest.raises(ConfigError, match="max_risk_pct"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_bool_risk_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nmax_risk_pct = true\n")
        with pytest.raises(ConfigError, match="max_risk_pct"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_wrong_risk_type_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nmax_risk_pct = \"2%\"\n")
        with pytest.raises(ConfigError, match="max_risk_pct"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_bad_clearing_time_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nclearing_times = [\"25:00\"]\n")
        with pytest.raises(ConfigError, match="clearing_times"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_empty_clearing_times_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nclearing_times = []\n")
        with pytest.raises(ConfigError, match="clearing_times"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_non_string_clearing_time_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\nclearing_times = [1405]\n")
        with pytest.raises(ConfigError, match="clearing_times"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))

    def test_non_string_journal_file_rejected(self, tmp_path):
        _write(tmp_path, "[trading]\njournal_file = 42\n")
        with pytest.raises(ConfigError, match="journal_file"):
            load_config(_defaults(), config_file=cfg_ref(tmp_path))


class TestTradingEnabled:
    def test_disabled_without_section(self):
        from src.config import _CONFIG

        saved = dict(_CONFIG)
        for key in ("initial_deposit", "max_risk_pct", "journal_file", "clearing_times"):
            _CONFIG.pop(key, None)
        try:
            assert trading_enabled() is False
        finally:
            _CONFIG.clear()
            _CONFIG.update(saved)

    def test_enabled_with_section_keys(self):
        from src.config import _CONFIG

        saved = dict(_CONFIG)
        _CONFIG["initial_deposit"] = 100000
        try:
            assert trading_enabled() is True
        finally:
            _CONFIG.clear()
            _CONFIG.update(saved)


class TestImportConstants:
    def test_constants_importable(self):
        assert isinstance(INITIAL_DEPOSIT, int) and INITIAL_DEPOSIT > 0
        assert 0 < MAX_RISK_PCT <= 100
        assert isinstance(JOURNAL_FILE, str) and JOURNAL_FILE
        assert isinstance(CLEARING_TIMES, list) and CLEARING_TIMES


def cfg_ref(tmp_path):
    """Заглушка: переиспользуем последний записанный файл robot.toml в tmp_path."""
    return tmp_path / "robot.toml"