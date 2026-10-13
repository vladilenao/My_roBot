"""Параметры live-ingestion: совместимый polling по умолчанию и отдельные сроки."""

from unittest.mock import Mock

import pytest

from src.bot import TradingBot
from src.bot.live_analysis import LiveAnalysis
from src.config_loader import ConfigError, load_config
from src.data.ingestion_settings import MarketDataSettings, SettingError, setting_names
from src.events import EventBus
from src.scheduler.timing import MultiTimeframeScheduler


def _defaults():
    return {"tick_timeout_secs": 65,
            **{f"market_data_{name}": value for name, value in MarketDataSettings().as_dict().items()}}


def _load(tmp_path, text):
    path = tmp_path / "robot.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(_defaults(), config_file=path, bundled_file=tmp_path / "missing.toml")


def test_old_config_without_new_keys_keeps_polling_profile(tmp_path):
    cfg = _load(tmp_path, "[market_data]\nexport_enabled = false\n")
    settings = MarketDataSettings.from_flat(cfg)
    assert settings.live_transport == "polling"
    assert settings.stream_fallback_polling is True
    assert settings.as_dict()["candle_publication_timeout_secs"] == 65


def test_stream_and_effective_limits_are_observable(tmp_path):
    cfg = _load(tmp_path, "[market_data]\nlive_transport = \"stream\"\nstream_buffer_max_events = 1000\n"
                          "recovery_round_timeout_secs = 600\n")
    settings = MarketDataSettings.from_flat(cfg)
    assert settings.live_transport == "stream"
    assert settings.as_dict()["stream_buffer_max_events"] == 1000
    assert settings.recovery_round_timeout_secs == 600


@pytest.mark.parametrize("line, key", [
    ("live_transport = \"websocket\"", "live_transport"),
    ("stream_buffer_max_events = -1", "stream_buffer_max_events"),
    ("stream_buffer_max_events = 1.5", "stream_buffer_max_events"),
    ("candle_publication_timeout_secs = 0", "candle_publication_timeout_secs"),
    ("recovery_round_timeout_secs = \"900\"", "recovery_round_timeout_secs"),
    ("stream_ping_interval_ms = 1000", "stream_ping_interval_ms"),
    ("stream_subscription_soft_limit = 301", "stream_subscription_soft_limit"),
    ("unary_requests_per_minute = 601", "unary_requests_per_minute"),
    ("stream_reconnect_jitter_pct = 120", "stream_reconnect_jitter_pct"),
    ("stream_fallback_polling = 1", "stream_fallback_polling"),
])
def test_invalid_value_is_rejected_with_key_name(tmp_path, line, key):
    with pytest.raises(ConfigError, match=key):
        _load(tmp_path, f"[market_data]\n{line}\n")


def test_infinite_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="recovery_backoff_max_secs"):
        _load(tmp_path, "[market_data]\nrecovery_backoff_max_secs = inf\n")


def test_inconsistent_reconnect_and_ping_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="stream_reconnect_initial_secs"):
        _load(tmp_path, "[market_data]\nstream_reconnect_initial_secs = 60\nstream_reconnect_max_secs = 30\n")
    with pytest.raises(ConfigError, match="ping"):
        _load(tmp_path, "[market_data]\nstream_ping_interval_ms = 60000\nstream_health_timeout_seconds = 45\n")


def test_changing_only_tick_timeout_keeps_live_parameters(tmp_path):
    cfg = _load(tmp_path, "[tick]\ntimeout_secs = 300\n")
    settings = MarketDataSettings.from_flat(cfg)
    assert cfg["tick_timeout_secs"] == 300
    assert settings.candle_publication_timeout_secs == 65
    assert settings.recovery_recheck_interval_secs == 300
    assert settings.status_repeat_interval_secs == 60


def test_live_analysis_uses_separate_publication_and_status_parameters():
    timeline = MultiTimeframeScheduler(["1m"], clock=lambda: None)
    settings = MarketDataSettings(candle_publication_timeout_secs=20, status_repeat_interval_secs=90)
    bot = TradingBot([], EventBus(), {}, Mock(live_session_enabled=True), timeline,
                     tick_timeout_secs=300, market_data_settings=settings)
    live = LiveAnalysis(bot)
    assert live.settings.candle_publication_timeout_secs == 20
    assert live.cadence._repeat.total_seconds() == 90


def test_dataclass_rejects_invalid_programmatic_profile():
    with pytest.raises(SettingError):
        MarketDataSettings(stream_buffer_max_bytes=0)
    assert set(setting_names()) >= {"live_transport", "candle_publication_timeout_secs",
                                    "recovery_round_timeout_secs", "recovery_recheck_interval_secs"}
