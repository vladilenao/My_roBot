import sys

import pytest

from src.config_loader import ConfigError, app_dir, load_config, validate_triple_screen_hierarchy
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


class TestTripleScreenSection:
    def _load(self, tmp_path, text):
        path = tmp_path / "robot.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(_defaults(), config_file=path)

    def test_section_parsed(self, tmp_path):
        cfg = self._load(
            tmp_path,
            '[strategies.filter.triple_screen]\n'
            'multiplier = 3\n'
            'oversold = 25\n'
            'macd_fast = 5\n',
        )

        assert cfg["triple_screen_params"] == {
            "multiplier": 3,
            "oversold": 25,
            "macd_fast": 5,
        }

    def test_empty_section_means_defaults(self, tmp_path):
        cfg = self._load(tmp_path, "[strategies.filter.triple_screen]\n")

        assert cfg.get("triple_screen_params") == {}

    def test_unknown_key_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="window"):
            self._load(tmp_path, "[strategies.filter.triple_screen]\nwindow = 5\n")

    def test_float_value_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="целыми числами"):
            self._load(tmp_path, '[strategies.filter.triple_screen]\nmultiplier = 5.0\n')

    def test_bool_value_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="целыми числами"):
            self._load(tmp_path, "[strategies.filter.triple_screen]\nmultiplier = true\n")

    def test_invalid_zones_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="oversold"):
            self._load(
                tmp_path,
                "[strategies.filter.triple_screen]\noversold = 90\noverbought = 20\n",
            )

    def test_multiplier_one_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="множитель"):
            self._load(tmp_path, "[strategies.filter.triple_screen]\nmultiplier = 1\n")

    def test_unknown_filter_subsection_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="triple_screen"):
            self._load(tmp_path, "[strategies.filter]\nbogus = { x = 1 }\n")


class TestTripleScreenHierarchyValidation:
    def test_compatible_tf_passes(self):
        bindings = {
            "SBER": [
                Assignment(
                    strategy="macd_rsi_stoch",
                    filter_profile="triple_screen",
                    timeframe="5m",
                )
            ]
        }
        validate_triple_screen_hierarchy(
            bindings, multiplier=5, ladder=("1m", "5m", "15m", "30m", "1h", "4h"), path=object()
        )

    def test_incompatible_tf_raises_config_error(self):
        from pathlib import Path

        bindings = {
            "SBER": [
                Assignment(
                    strategy="macd_rsi_stoch",
                    filter_profile="triple_screen",
                    timeframe="1M",
                )
            ]
        }
        with pytest.raises(ConfigError, match="несовместима с профилем triple_screen"):
            validate_triple_screen_hierarchy(
                bindings,
                multiplier=5,
                ladder=("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"),
                path=Path("robot.toml"),
            )

    def test_config_flow_binding_compatible(self, tmp_path):
        """Интеграция: привязка {filter = triple_screen, tf = 5m} проходит весь путь конфига."""
        from src.config import _to_assignments, TIMEFRAMES, TRIPLE_SCREEN_PARAMS

        path = tmp_path / "robot.toml"
        path.write_text(
            "[strategies.share.SBER]\n"
            'strategies = [{ name = "macd_rsi_stoch", filter = "triple_screen", tf = "5m" }]\n',
            encoding="utf-8",
        )
        cfg = load_config(_defaults(), config_file=path)
        bindings = _to_assignments(cfg["share_strategies"])

        result = bindings["SBER"][0]
        assert (result.strategy, result.filter_profile, result.timeframe) == (
            "macd_rsi_stoch",
            "triple_screen",
            "5m",
        )
        validate_triple_screen_hierarchy(
            bindings, TRIPLE_SCREEN_PARAMS.multiplier, tuple(TIMEFRAMES), path
        )

    def test_config_flow_binding_incompatible_rejected(self, tmp_path):
        """Интеграция: привязка с tf = 1M отсекается fail-fast на этапе конфига."""
        from src.config import _to_assignments, TIMEFRAMES, TRIPLE_SCREEN_PARAMS

        path = tmp_path / "robot.toml"
        path.write_text(
            "[strategies.share.SBER]\n"
            'strategies = [{ name = "macd_rsi_stoch", filter = "triple_screen", tf = "1M" }]\n',
            encoding="utf-8",
        )
        cfg = load_config(_defaults(), config_file=path)
        bindings = _to_assignments(cfg["share_strategies"])

        with pytest.raises(ConfigError, match="несовместима с профилем triple_screen"):
            validate_triple_screen_hierarchy(
                bindings, TRIPLE_SCREEN_PARAMS.multiplier, tuple(TIMEFRAMES), path
            )