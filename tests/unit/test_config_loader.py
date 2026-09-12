import sys

import pytest

from src.config_loader import ConfigError, app_dir, load_config
from src.strategies.contracts import Assignment


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
        "share_strategies": {"SBER": {"strategies": ["macd_rsi_stoch"], "timeframe": None}},
        "future_strategies": {
            "NG": {"strategies": ["macd_rsi_stoch"], "timeframe": None},
            "ED": {"strategies": ["flat_triangle"], "timeframe": None},
        },
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


def _write(tmp_path, body: str):
    cfg = tmp_path / "robot.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


class TestHybridStrategyAssignments:
    def test_string_entry_passes_through(self, tmp_path):
        cfg = _write(tmp_path, '[strategies.share.SBER]\nstrategies = ["flat_triangle"]\n')

        result = load_config(_defaults(), config_file=cfg)

        assert result["share_strategies"] == {
            "SBER": {"strategies": ["flat_triangle"], "timeframe": None}
        }

    def test_inline_table_with_filter_passes_through(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.future.ED]\n'
            'strategies = [{ name = "flat_triangle", filter = "raw" }]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["future_strategies"] == {
            "ED": {"strategies": [{"name": "flat_triangle", "filter": "raw"}], "timeframe": None}
        }

    def test_inline_table_without_name_raises(self, tmp_path):
        cfg = _write(
            tmp_path, '[strategies.share.SBER]\nstrategies = [{ filter = "raw" }]\n'
        )

        with pytest.raises(ConfigError, match="name"):
            load_config(_defaults(), config_file=cfg)

    def test_inline_tf_key_is_accepted(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\n'
            'strategies = [{ name = "macd_rsi_stoch", tf = "15m" }]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["share_strategies"]["SBER"]["strategies"] == [
            {"name": "macd_rsi_stoch", "tf": "15m"}
        ]

    def test_ticker_timeframe_key_is_accepted(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\ntimeframe = "15m"\nstrategies = ["flat_triangle"]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["share_strategies"]["SBER"]["timeframe"] == "15m"

    def test_unknown_inline_key_raises(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\n'
            'strategies = [{ name = "macd_rsi_stoch", window = 3 }]\n',
        )

        with pytest.raises(ConfigError, match="window"):
            load_config(_defaults(), config_file=cfg)

    def test_flat_list_syntax_raises(self, tmp_path):
        cfg = _write(tmp_path, '[strategies.share]\nSBER = ["macd_rsi_stoch"]\n')

        with pytest.raises(ConfigError):
            load_config(_defaults(), config_file=cfg)

    def test_non_string_entry_type_raises(self, tmp_path):
        cfg = _write(tmp_path, '[strategies.share.SBER]\nstrategies = [42]\n')

        with pytest.raises(ConfigError):
            load_config(_defaults(), config_file=cfg)

    def test_ticker_table_without_strategies_key_raises(self, tmp_path):
        cfg = _write(tmp_path, '[strategies.share.SBER]\n')

        with pytest.raises(ConfigError, match="strategies"):
            load_config(_defaults(), config_file=cfg)


class TestToAssignments:
    """Каскад гибридной записи в типизированные Assignment (config.py)."""

    @staticmethod
    def _table(strategies, timeframe=None):
        return {"strategies": strategies, "timeframe": timeframe}

    def test_string_entry_gets_default_profile_and_global_tf(self):
        from src.config import _to_assignments

        assert _to_assignments({"SBER": self._table(["flat_triangle"])}) == {
            "SBER": [Assignment("flat_triangle", "basic_levels", timeframe="1h")]
        }

    def test_inline_table_overrides_profile(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"ED": self._table([{"name": "flat_triangle", "filter": "raw"}])}
        )

        assert result["ED"] == [Assignment("flat_triangle", "raw", timeframe="1h")]

    def test_inline_table_without_filter_gets_default_profile(self):
        from src.config import _to_assignments

        result = _to_assignments({"ED": self._table([{"name": "flat_triangle"}])})

        assert result["ED"][0].filter_profile == "basic_levels"

    def test_duplicate_names_with_distinct_profiles(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"name": "macd_rsi_stoch", "filter": "raw"}, "macd_rsi_stoch"])}
        )

        assert [a.strategy for a in result["SBER"]] == ["macd_rsi_stoch", "macd_rsi_stoch"]
        assert [a.filter_profile for a in result["SBER"]] == ["raw", "basic_levels"]

    def test_inline_tf_beats_ticker_timeframe(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"name": "macd_rsi_stoch", "tf": "15m"}], timeframe="1h")}
        )

        assert result["SBER"][0].timeframe == "15m"

    def test_ticker_timeframe_beats_global(self):
        from src.config import _to_assignments

        result = _to_assignments({"SBER": self._table(["flat_triangle"], timeframe="15m")})

        assert result["SBER"][0].timeframe == "15m"

    def test_inline_tf_falls_back_to_ticker_then_global(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"name": "macd_rsi_stoch", "filter": "raw"}], timeframe="5m")}
        )

        assert result["SBER"][0].timeframe == "5m"

    def test_invalid_inline_tf_raises_with_allowed_list(self):
        from src.config import _to_assignments

        with pytest.raises(ConfigError, match="2h"):
            _to_assignments({"SBER": self._table([{"name": "macd_rsi_stoch", "tf": "2h"}])})

    def test_invalid_ticker_timeframe_raises(self):
        from src.config import _to_assignments

        with pytest.raises(ConfigError, match="2h"):
            _to_assignments({"SBER": self._table(["flat_triangle"], timeframe="2h")})