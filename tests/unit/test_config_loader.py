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
        "notifier_channels": ["console"],
        "notifier_console_events": ["decision"],
        "notifier_telegram_events": ["signal"],
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


def test_bundled_default_si_two_ma_cloud_bindings_on_5m():
    """Бандл default.toml: сценарий сравнения на SI — две привязки
    ma_cloud_rsi_macd на 5m с профилями raw и triple_screen."""
    bundled = app_dir() / "default.toml"
    cfg = load_config(
        _defaults(),
        config_file=bundled.parent / "no_such_robot.toml",
        bundled_file=bundled,
    )

    si = cfg["future_strategies"]["SI"]["strategies"]
    assert si == [
        {"id": "future-si-ma-raw", "name": "ma_cloud_rsi_macd", "management": "ma_cloud", "filter": "raw", "tf": "5m", "priority": 10},
        {"id": "future-si-ma-triple", "name": "ma_cloud_rsi_macd", "management": "ma_cloud", "filter": "triple_screen", "tf": "5m", "priority": 10},
    ]


class TestTradeManagementConfiguration:
    def test_unknown_profile_key_is_rejected(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.levels_rr]\n'
            'type = "levels_rr"\n'
            'target_R = [1.0, 2.0]\n'
            'shares = [0.5, 0.5]\n'
            'unexpected = 1\n',
        )
        with pytest.raises(ConfigError, match="unexpected"):
            load_config(_defaults(), config_file=cfg)

    def test_duplicate_ids_are_rejected_before_config_import(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\n'
            'strategies = [{ id = "duplicate", name = "flat_triangle", management = "levels_rr" }]\n'
            '[strategies.future.NG]\n'
            'strategies = [{ id = "duplicate", name = "macd_rsi_stoch", management = "levels_rr" }]\n',
        )
        with pytest.raises(ConfigError, match="повторяющийся id"):
            load_config(_defaults(), config_file=cfg)

    @pytest.mark.parametrize("profile", ["levels_rr", "atr_trend", "ma_cloud", "pattern_targets"])
    def test_stop_geometry_is_accepted_by_every_profile(self, tmp_path, profile):
        body = {
            "levels_rr": 'target_R = [1.0, 2.0]\nshares = [0.5, 0.5]\n',
            "atr_trend": 'atr_period = 14\ninitial_k = 2.0\ntrail_k = 2.0\ntp1_R = 1.0\ntp1_share = 0.5\n',
            "ma_cloud": 'ma_fast_period = 10\nma_slow_period = 40\n',
            "pattern_targets": 'fractions_to_D = [0.5, 1.0]\nshares = [0.5, 0.5]\n',
        }[profile]
        cfg = _write(
            tmp_path,
            f'[trade_management.profiles.{profile}]\ntype = "{profile}"\n{body}'
            'min_stop_atr = 1.5\nmin_stop_ticks = 10\nstop_beyond_bar = 1.0\nmax_stop_atr = 6.0\n',
        )

        loaded = load_config(_defaults(), config_file=cfg)

        assert loaded["trade_management_profiles"][profile]["min_stop_ticks"] == 10

    def test_profile_without_geometry_keeps_only_its_own_keys(self, tmp_path):
        """Отсутствие геометрии — не ошибка: правило берёт дефолты, а не выдумывает ключи."""
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.levels_rr]\n'
            'type = "levels_rr"\ntarget_R = [1.0, 2.0]\nshares = [0.5, 0.5]\n',
        )

        loaded = load_config(_defaults(), config_file=cfg)

        assert "min_stop_atr" not in loaded["trade_management_profiles"]["levels_rr"]

    def test_unknown_geometry_key_names_the_key(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.levels_rr]\n'
            'type = "levels_rr"\ntarget_R = [1.0, 2.0]\nshares = [0.5, 0.5]\n'
            'min_stop_atr_multiplier = 1.5\n',
        )
        with pytest.raises(ConfigError, match="min_stop_atr_multiplier") as error:
            load_config(_defaults(), config_file=cfg)
        assert "levels_rr" in str(error.value)
        assert "robot.toml" in str(error.value)

    @pytest.mark.parametrize("body,match", [
        ('min_stop_atr = -0.1\n', "min_stop_atr"),
        ('min_stop_ticks = 1.5\n', "min_stop_ticks"),
        ('min_stop_ticks = -2\n', "min_stop_ticks"),
        ('stop_beyond_bar = -1\n', "stop_beyond_bar"),
        ('max_stop_atr = -3\n', "max_stop_atr"),
        ('min_stop_atr = 4.0\nmax_stop_atr = 3.0\n', "max_stop_atr"),
    ])
    def test_stop_geometry_bounds_are_validated(self, tmp_path, body, match):
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.levels_rr]\n'
            'type = "levels_rr"\ntarget_R = [1.0, 2.0]\nshares = [0.5, 0.5]\n' + body,
        )
        with pytest.raises(ConfigError, match=match):
            load_config(_defaults(), config_file=cfg)

    def test_min_trade_risk_pct_cannot_exceed_the_portfolio_limit(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trading.risk_limits]\nportfolio_pct = 2.0\nmin_trade_risk_pct = 3.0\n',
        )
        with pytest.raises(ConfigError, match="min_trade_risk_pct"):
            load_config(_defaults(), config_file=cfg)

    def test_min_trade_risk_pct_may_be_zero(self, tmp_path):
        """Ноль — это осознанное «не проверять», а не запрещённое значение."""
        cfg = _write(
            tmp_path,
            '[trading.risk_limits]\nportfolio_pct = 2.0\nmin_trade_risk_pct = 0.0\n',
        )

        loaded = load_config(_defaults(), config_file=cfg)

        assert loaded["risk_limits"]["min_trade_risk_pct"] == 0.0

    def test_min_trade_risk_pct_must_be_non_negative(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trading.risk_limits]\nportfolio_pct = 2.0\nmin_trade_risk_pct = -0.5\n',
        )
        with pytest.raises(ConfigError, match="min_trade_risk_pct"):
            load_config(_defaults(), config_file=cfg)

    @pytest.mark.parametrize("key", ["commission", "slippage"])
    def test_costs_are_validated_as_non_negative_numbers(self, tmp_path, key):
        negative = _write(
            tmp_path / "negative", f'[trading.risk_limits]\n{key} = -1.0\n',
        )
        with pytest.raises(ConfigError, match=key):
            load_config(_defaults(), config_file=negative)

        zero = _write(tmp_path / "zero", f'[trading.risk_limits]\n{key} = 0.0\n')
        loaded = load_config(_defaults(), config_file=zero)
        assert loaded["risk_limits"][key] == 0.0

    def test_nonfinite_profile_and_risk_values_are_rejected(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.atr_trend]\n'
            'type = "atr_trend"\natr_period = 14\ninitial_k = nan\n'
            'trail_k = 2.0\ntp1_R = 1.0\ntp1_share = 0.5\n'
            '[trading.risk_limits]\nportfolio_pct = nan\n',
        )
        with pytest.raises(ConfigError, match="initial_k"):
            load_config(_defaults(), config_file=cfg)

    def test_target_shares_and_warmup_are_validated(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[trade_management.profiles.levels_rr]\n'
            'type = "levels_rr"\ntarget_R = [1.0, 2.0]\nshares = [0.75, 0.75]\n',
        )
        with pytest.raises(ConfigError, match="shares"):
            load_config(_defaults(), config_file=cfg)

        cfg = _write(
            tmp_path,
            '[trade_management.profiles.ma_cloud]\n'
            'type = "ma_cloud"\nma_fast_period = 40\nma_slow_period = 10\n',
        )
        with pytest.raises(ConfigError, match="прогрева"):
            load_config(_defaults(), config_file=cfg)

    def test_pattern_profile_requires_compatible_strategy(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\n'
            'strategies = [{ id = "sber-pattern", name = "flat_triangle", management = "pattern_targets" }]\n'
            '[trade_management.profiles.pattern_targets]\n'
            'type = "pattern_targets"\nbuffer_ticks = 1\n'
            'fractions_to_D = [0.5, 1.0]\nshares = [0.5, 0.5]\n',
        )
        with pytest.raises(ConfigError, match="несовместим"):
            load_config(_defaults(), config_file=cfg)


class TestPortfolioEconomicsConfiguration:
    @pytest.mark.parametrize("body,key", [
        ("trade_pct = 0", "trade_pct"),
        ("instrument_pct = 4", "instrument_pct"),
        ("groups = {}", "groups"),
        ("trade_pct = 2\ninstrument_pct = 4\nportfolio_pct = 6\ngroups = { energy = 4 }", "trade_pct"),
    ])
    def test_legacy_limits_require_explicit_migration(self, tmp_path, body, key):
        path = _write(tmp_path, "[trading.risk_limits]\n" + body + "\n")
        with pytest.raises(ConfigError, match=key) as error:
            load_config({}, config_file=path)
        assert str(path) in str(error.value)
        assert "portfolio_pct" in str(error.value)
        assert "автоматически не переносятся" in str(error.value)

    @pytest.mark.parametrize("value", ["0", "1", "2", "100"])
    def test_global_percentage_range(self, tmp_path, value):
        cfg = load_config({}, config_file=_write(tmp_path, f"[trading.risk_limits]\nportfolio_pct = {value}\n"))
        assert cfg["risk_limits"]["portfolio_pct"] == float(value)

    @pytest.mark.parametrize("value", ["-1", "101", "nan", "inf", "true", '"2"'])
    def test_invalid_global_percentage_names_key(self, tmp_path, value):
        with pytest.raises(ConfigError, match="portfolio_pct"):
            load_config({}, config_file=_write(tmp_path, f"[trading.risk_limits]\nportfolio_pct = {value}\n"))

    def test_missing_values_receive_positive_costs_and_global_defaults(self, tmp_path):
        cfg = load_config({}, config_file=_write(tmp_path, "[trading.risk_limits]\nmax_qty = 3\n"))
        risk = cfg["risk_limits"]
        assert (risk["portfolio_pct"], risk["commission"], risk["slippage"]) == (2, 1.5, 1)
        assert (risk["min_risk_cost_ratio"], risk["min_net_payoff"], risk["max_slippage_r"]) == (2, 1.5, 0.25)

    @pytest.mark.parametrize("key", ["min_risk_cost_ratio", "min_net_payoff", "max_slippage_r", "commission", "slippage"])
    @pytest.mark.parametrize("value", ["-1", "nan", "inf", "true"])
    def test_economics_values_are_finite_nonnegative_numbers(self, tmp_path, key, value):
        with pytest.raises(ConfigError, match=key):
            load_config({}, config_file=_write(tmp_path, f"[trading.risk_limits]\n{key} = {value}\n"))

    def test_explicit_zero_values_override_bundled_values(self, tmp_path):
        bundled = app_dir() / "default.toml"
        cfg = load_config({}, bundled_file=bundled, config_file=_write(
            tmp_path, "[trading.risk_limits]\ncommission = 0\nslippage = 0\n"
            "min_risk_cost_ratio = 0\nmin_net_payoff = 0\nmax_slippage_r = 0\n",
        ))
        risk = cfg["risk_limits"]
        assert all(risk[key] == 0 for key in ("commission", "slippage", "min_risk_cost_ratio", "min_net_payoff", "max_slippage_r"))
        assert risk["portfolio_pct"] == 2

    @pytest.mark.parametrize("key", ["liquidity_floor", "algorithm_version"])
    def test_unknown_economics_keys_are_not_ignored(self, tmp_path, key):
        with pytest.raises(ConfigError, match=key):
            load_config({}, config_file=_write(tmp_path, f"[trading.risk_limits]\n{key} = 1\n"))

    def test_legacy_atr_pair_overrides_new_bundled_ladder(self, tmp_path):
        cfg = load_config({}, bundled_file=app_dir() / "default.toml", config_file=_write(
            tmp_path, '[trade_management.profiles.atr_trend]\ntp1_R = 1.2\ntp1_share = 0.5\n',
        ))
        profile = cfg["trade_management_profiles"]["atr_trend"]
        assert profile["target_R"] == [1.2]
        assert profile["shares"] == [0.5]
        assert "tp1_R" not in profile and "tp1_share" not in profile
        assert profile["atr_period"] == 14
        assert "ma_cloud" in cfg["trade_management_profiles"]

    @pytest.mark.parametrize("body,key", [
        ("tp1_R = 1", "tp1_share"),
        ("tp1_share = 0.5", "tp1_R"),
        ("target_R = [1, 2]\ntp1_R = 1\ntp1_share = 0.5", "конфликт"),
        ("shares = [0.25, 0.25]\ntp1_share = 0.5", "конфликт"),
        ("target_R = [1, 2]\nshares = [0.5, 0.5]", "shares"),
        ("target_R = [2, 1]\nshares = [0.25, 0.25]", "target_R"),
        ("target_R = [1, 2]\nshares = [0.5]", "shares"),
        ("target_R = [true, 2]\nshares = [0.25, 0.25]", "target_R"),
        ("target_R = [1, 2]\nshares = [nan, 0.25]", "shares"),
    ])
    def test_invalid_ladder_forms_fail_before_cycle(self, tmp_path, body, key):
        with pytest.raises(ConfigError, match=key):
            load_config({}, config_file=_write(
                tmp_path, '[trade_management.profiles.atr_trend]\ntype = "atr_trend"\natr_period = 14\n' + body + "\n",
            ))

    @pytest.mark.parametrize("profile", ["levels_rr", "pattern_targets"])
    @pytest.mark.parametrize("value", ["0", "1.5", "-1", "nan", "true"])
    def test_be_threshold_is_nonnegative_and_can_be_disabled(self, tmp_path, profile, value):
        path = _write(tmp_path, f"[trade_management.profiles.{profile}]\nmin_be_r = {value}\n")
        if value in {"0", "1.5"}:
            cfg = load_config({}, bundled_file=app_dir() / "default.toml", config_file=path)
            assert cfg["trade_management_profiles"][profile]["min_be_r"] == float(value)
        else:
            with pytest.raises(ConfigError, match="min_be_r"):
                load_config({}, bundled_file=app_dir() / "default.toml", config_file=path)


class TestDirectionsConfiguration:
    def test_absent_section_allows_both_sides(self, tmp_path):
        cfg = load_config({}, config_file=_write(tmp_path, "[trading.risk_limits]\nportfolio_pct = 2\n"))
        assert cfg.get("directions") is None

    def test_partial_table_keeps_only_supplied_types(self, tmp_path):
        cfg = load_config({}, config_file=_write(tmp_path, "[trading.directions]\nshare = [\"long\"]\n"))
        assert cfg["directions"] == {"share": ["long"]}

    def test_future_and_share_explicit_list(self, tmp_path):
        cfg = load_config({}, config_file=_write(
            tmp_path, "[trading.directions]\nfuture = [\"long\", \"short\"]\nshare = [\"long\"]\n",
        ))
        assert cfg["directions"] == {"future": ["long", "short"], "share": ["long"]}

    @pytest.mark.parametrize("body,match", [
        ('share = ["szhort"]', "szhort"),
        ('share = ["LONG"]', "long"),
        ("share = []", "непустым"),
        ('share = "long"', "непустым"),
    ])
    def test_invalid_directions_raise_config_error_with_key(self, tmp_path, body, match):
        with pytest.raises(ConfigError, match=match) as error:
            load_config({}, config_file=_write(tmp_path, f"[trading.directions]\n{body}\n"))
        assert "trading.directions" in str(error.value)

    def test_duplicate_directions_are_deduplicated(self, tmp_path):
        cfg = load_config({}, config_file=_write(tmp_path, "[trading.directions]\nshare = [\"long\", \"long\"]\n"))
        assert cfg["directions"] == {"share": ["long"]}


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


def test_invalid_legacy_notifier_channel_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannel = \"sms\"\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_unknown_channel_in_channels_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannels = [\"sms\"]\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_empty_channels_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannels = []\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_duplicate_channel_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannels = [\"console\", \"console\"]\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_legacy_channel_becomes_single_element_channels(tmp_path):
    legacy = tmp_path / "robot.toml"
    legacy.write_text("[notifier]\nchannel = \"telegram\"\n", encoding="utf-8")

    config = load_config(_defaults(), config_file=legacy)

    assert config["notifier_channels"] == ["telegram"]
    assert "notifier" not in config


def test_legacy_channel_and_channels_together_raise_config_error(tmp_path):
    both = tmp_path / "robot.toml"
    both.write_text(
        "[notifier]\nchannel = \"telegram\"\nchannels = [\"console\"]\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=both)


def test_per_channel_events_are_parsed(tmp_path):
    ok = tmp_path / "robot.toml"
    ok.write_text(
        "[notifier]\nchannels = [\"console\", \"telegram\"]\n\n"
        "[notifier.console]\nevents = [\"decision\", \"signal\"]\n\n"
        "[notifier.telegram]\nevents = [\"trade_closed\"]\n",
        encoding="utf-8",
    )

    config = load_config(_defaults(), config_file=ok)

    assert config["notifier_channels"] == ["console", "telegram"]
    assert config["notifier_console_events"] == ["decision", "signal"]
    assert config["notifier_telegram_events"] == ["trade_closed"]


def test_telegram_request_timeout_is_parsed(tmp_path):
    ok = tmp_path / "robot.toml"
    ok.write_text(
        "[notifier]\nchannels = [\"telegram\"]\n\n"
        "[notifier.telegram]\nevents = [\"signal\"]\nrequest_timeout = 30\n",
        encoding="utf-8",
    )

    config = load_config(_defaults(), config_file=ok)

    assert config["notifier_telegram_request_timeout"] == 30


def test_request_timeout_in_console_subsection_raises(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text(
        "[notifier]\nchannels = [\"console\"]\n\n"
        "[notifier.console]\nevents = [\"signal\"]\nrequest_timeout = 30\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="request_timeout"):
        load_config(_defaults(), config_file=bad)


def test_nonpositive_request_timeout_raises(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text(
        "[notifier]\nchannels = [\"telegram\"]\n\n"
        "[notifier.telegram]\nevents = [\"signal\"]\nrequest_timeout = 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="request_timeout"):
        load_config(_defaults(), config_file=bad)


def test_max_transport_attempts_is_parsed(tmp_path):
    ok = tmp_path / "robot.toml"
    ok.write_text(
        "[notifier]\nchannels = [\"telegram\"]\n\n"
        "[notifier.telegram]\nevents = [\"signal\"]\nmax_transport_attempts = 3\n",
        encoding="utf-8",
    )

    config = load_config(_defaults(), config_file=ok)

    assert config["notifier_telegram_max_transport_attempts"] == 3


def test_max_transport_attempts_in_console_subsection_raises(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text(
        "[notifier]\nchannels = [\"console\"]\n\n"
        "[notifier.console]\nevents = [\"signal\"]\nmax_transport_attempts = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_transport_attempts"):
        load_config(_defaults(), config_file=bad)


def test_zero_max_transport_attempts_raises(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text(
        "[notifier]\nchannels = [\"telegram\"]\n\n"
        "[notifier.telegram]\nevents = [\"signal\"]\nmax_transport_attempts = 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_transport_attempts"):
        load_config(_defaults(), config_file=bad)


def test_unknown_event_type_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text(
        "[notifier]\nchannels = [\"console\"]\n\n[notifier.console]\nevents = [\"nope\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_missing_events_key_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannels = [\"console\"]\n\n[notifier.console]\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_unknown_notifier_subsection_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[notifier]\nchannels = [\"console\"]\n\n[notifier.sms]\nevents = [\"signal\"]\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


def test_wrong_type_raises_config_error(tmp_path):
    bad = tmp_path / "robot.toml"
    bad.write_text("[robot]\nsleep_seconds = \"fast\"\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(_defaults(), config_file=bad)


class TestCatchUpBars:
    def test_key_optional_in_tick_section(self, tmp_path):
        cfg = load_config(_defaults(), config_file=_write(tmp_path, "[tick]\npoll_secs = 1\n"))
        assert "tick_catch_up_bars" not in cfg

    def test_valid_int_value(self, tmp_path):
        cfg = load_config(_defaults(), config_file=_write(tmp_path, "[tick]\ncatch_up_bars = 2\n"))
        assert cfg["tick_catch_up_bars"] == 2

    def test_wrong_type_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[tick]\ncatch_up_bars = \"2\"\n")
        with pytest.raises(ConfigError, match="tick_catch_up_bars"):
            load_config(_defaults(), config_file=cfg)

    def test_negative_value_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[tick]\ncatch_up_bars = -1\n")
        with pytest.raises(ConfigError, match="catch_up_bars"):
            load_config(_defaults(), config_file=cfg)

    def test_bool_value_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[tick]\ncatch_up_bars = true\n")
        with pytest.raises(ConfigError, match="catch_up_bars"):
            load_config(_defaults(), config_file=cfg)


class TestContractExpiryBlockDays:
    def test_key_optional_in_trading_section(self, tmp_path):
        cfg = load_config(_defaults(), config_file=_write(tmp_path, "[trading]\ninitial_deposit = 100000\n"))
        assert "contract_expiry_block_days" not in cfg

    def test_key_overrides_default(self, tmp_path):
        cfg = load_config(
            _defaults(), config_file=_write(tmp_path, "[trading]\ncontract_expiry_block_days = 5\n")
        )
        assert cfg["contract_expiry_block_days"] == 5

    def test_zero_is_allowed(self, tmp_path):
        cfg = load_config(
            _defaults(), config_file=_write(tmp_path, "[trading]\ncontract_expiry_block_days = 0\n")
        )
        assert cfg["contract_expiry_block_days"] == 0

    def test_wrong_type_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[trading]\ncontract_expiry_block_days = \"2\"\n")
        with pytest.raises(ConfigError, match="contract_expiry_block_days"):
            load_config(_defaults(), config_file=cfg)

    def test_negative_value_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[trading]\ncontract_expiry_block_days = -1\n")
        with pytest.raises(ConfigError, match="contract_expiry_block_days"):
            load_config(_defaults(), config_file=cfg)

    def test_bool_value_rejected(self, tmp_path):
        cfg = _write(tmp_path, "[trading]\ncontract_expiry_block_days = true\n")
        with pytest.raises(ConfigError, match="contract_expiry_block_days"):
            load_config(_defaults(), config_file=cfg)


def _write(tmp_path, body: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "robot.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


class TestHybridStrategyAssignments:
    def test_string_entry_is_rejected(self, tmp_path):
        cfg = _write(tmp_path, '[strategies.share.SBER]\nstrategies = ["flat_triangle"]\n')

        with pytest.raises(ConfigError, match="id и management"):
            load_config(_defaults(), config_file=cfg)

    def test_inline_table_with_filter_passes_through(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.future.ED]\n'
            'strategies = [{ id = "ed-flat", name = "flat_triangle", management = "levels_rr", filter = "raw" }]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["future_strategies"] == {
            "ED": {"strategies": [{"id": "ed-flat", "name": "flat_triangle", "management": "levels_rr", "filter": "raw"}], "timeframe": None}
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
            'strategies = [{ id = "sber-macd", name = "macd_rsi_stoch", management = "levels_rr", tf = "15m" }]\n',
        )

        result = load_config(_defaults(), config_file=cfg)

        assert result["share_strategies"]["SBER"]["strategies"] == [
            {"id": "sber-macd", "name": "macd_rsi_stoch", "management": "levels_rr", "tf": "15m"}
        ]

    def test_ticker_timeframe_key_is_accepted(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[strategies.share.SBER]\ntimeframe = "15m"\nstrategies = [{ id = "sber-flat", name = "flat_triangle", management = "levels_rr" }]\n',
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

    def test_explicit_entry_preserves_identity_and_default_priority(self):
        from src.config import _to_assignments

        assert _to_assignments({"SBER": self._table([{"id": "share-flat", "name": "flat_triangle", "management": "levels_rr"}])}) == {
            "SBER": [Assignment(id="share-flat", strategy="flat_triangle", management="levels_rr", filter_profile="basic_levels", timeframe="1h")]
        }

    def test_inline_table_overrides_profile(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"ED": self._table([{"id": "ed-flat", "name": "flat_triangle", "management": "levels_rr", "filter": "raw"}])}
        )

        assert result["ED"] == [Assignment(id="ed-flat", strategy="flat_triangle", management="levels_rr", filter_profile="raw", timeframe="1h")]

    def test_inline_table_without_filter_gets_default_profile(self):
        from src.config import _to_assignments

        result = _to_assignments({"ED": self._table([{"id": "ed-flat", "name": "flat_triangle", "management": "levels_rr"}])})

        assert result["ED"][0].filter_profile == "basic_levels"

    def test_duplicate_names_with_distinct_profiles(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"id": "sber-macd-raw", "name": "macd_rsi_stoch", "management": "levels_rr", "filter": "raw"}, {"id": "sber-macd-basic", "name": "macd_rsi_stoch", "management": "levels_rr"}])}
        )

        assert [a.strategy for a in result["SBER"]] == ["macd_rsi_stoch", "macd_rsi_stoch"]
        assert [a.filter_profile for a in result["SBER"]] == ["raw", "basic_levels"]

    def test_duplicate_assignment_id_is_rejected_across_instruments(self):
        from src.config import _to_assignments

        with pytest.raises(ConfigError, match="повторяющийся id"):
            _to_assignments({
                "SBER": self._table([{"id": "shared", "name": "macd_rsi_stoch", "management": "levels_rr"}]),
                "ED": self._table([{"id": "shared", "name": "flat_triangle", "management": "levels_rr"}]),
            })

    def test_duplicate_assignment_id_across_sources_is_rejected(self):
        from src.config import _to_assignments, _validate_global_assignment_ids

        share = _to_assignments({"SBER": self._table([{"id": "shared", "name": "macd_rsi_stoch", "management": "levels_rr"}])})
        future = _to_assignments({"ED": self._table([{"id": "shared", "name": "flat_triangle", "management": "levels_rr"}])})

        with pytest.raises(ConfigError, match="глобально уникальным"):
            _validate_global_assignment_ids(share, future)

    def test_inline_tf_beats_ticker_timeframe(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"id": "sber-macd", "name": "macd_rsi_stoch", "management": "levels_rr", "tf": "15m"}], timeframe="1h")}
        )

        assert result["SBER"][0].timeframe == "15m"

    def test_ticker_timeframe_beats_global(self):
        from src.config import _to_assignments

        result = _to_assignments({"SBER": self._table([{"id": "sber-flat", "name": "flat_triangle", "management": "levels_rr"}], timeframe="15m")})

        assert result["SBER"][0].timeframe == "15m"

    def test_inline_tf_falls_back_to_ticker_then_global(self):
        from src.config import _to_assignments

        result = _to_assignments(
            {"SBER": self._table([{"id": "sber-macd", "name": "macd_rsi_stoch", "management": "levels_rr", "filter": "raw"}], timeframe="5m")}
        )

        assert result["SBER"][0].timeframe == "5m"

    def test_invalid_inline_tf_raises_with_allowed_list(self):
        from src.config import _to_assignments

        with pytest.raises(ConfigError, match="2h"):
            _to_assignments({"SBER": self._table([{"id": "sber-macd", "name": "macd_rsi_stoch", "management": "levels_rr", "tf": "2h"}])})

    def test_invalid_ticker_timeframe_raises(self):
        from src.config import _to_assignments

        with pytest.raises(ConfigError, match="2h"):
            _to_assignments({"SBER": self._table([{"id": "sber-flat", "name": "flat_triangle", "management": "levels_rr"}], timeframe="2h")})


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
                    id="sber-macd",
                    strategy="macd_rsi_stoch",
                    management="levels_rr",
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
                    id="sber-macd",
                    strategy="macd_rsi_stoch",
                    management="levels_rr",
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
            'strategies = [{ id = "sber-macd", name = "macd_rsi_stoch", management = "levels_rr", filter = "triple_screen", tf = "5m" }]\n',
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
            'strategies = [{ id = "sber-macd", name = "macd_rsi_stoch", management = "levels_rr", filter = "triple_screen", tf = "1M" }]\n',
            encoding="utf-8",
        )
        cfg = load_config(_defaults(), config_file=path)
        bindings = _to_assignments(cfg["share_strategies"])

        with pytest.raises(ConfigError, match="несовместима с профилем triple_screen"):
            validate_triple_screen_hierarchy(
                bindings, TRIPLE_SCREEN_PARAMS.multiplier, tuple(TIMEFRAMES), path
            )
