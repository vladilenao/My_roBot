"""Конфигурация публикации свечных паттернов в Redis."""

import pytest

from src.config_loader import ConfigError, load_config


def _defaults():
    return {
        "market_structure_patterns_enabled": False,
        "market_structure_redis_host": "127.0.0.1",
        "market_structure_redis_port": 6379,
        "market_structure_redis_db": 0,
        "market_structure_queue_maxsize": 1024,
        "market_structure_retention_days": 90,
    }


def test_market_structure_settings_are_read_from_robot_toml(tmp_path):
    path = tmp_path / "robot.toml"
    path.write_text(
        "[market_structure]\n"
        "patterns_enabled = true\n"
        "redis_host = \"redis.internal\"\n"
        "redis_port = 6380\n"
        "redis_db = 2\n"
        "queue_maxsize = 500\n"
        "retention_days = 90\n",
        encoding="utf-8",
    )

    config = load_config(_defaults(), config_file=path, bundled_file=tmp_path / "missing.toml")

    assert config == {
        "market_structure_patterns_enabled": True,
        "market_structure_redis_host": "redis.internal",
        "market_structure_redis_port": 6380,
        "market_structure_redis_db": 2,
        "market_structure_queue_maxsize": 500,
        "market_structure_retention_days": 90,
    }


@pytest.mark.parametrize("key, value", [
    ("redis_port", "0"),
    ("redis_port", "65536"),
    ("redis_db", "-1"),
    ("queue_maxsize", "0"),
    ("retention_days", "0"),
])
def test_market_structure_rejects_invalid_numeric_values(tmp_path, key, value):
    path = tmp_path / "robot.toml"
    path.write_text(f"[market_structure]\n{key} = {value}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=key):
        load_config(_defaults(), config_file=path, bundled_file=tmp_path / "missing.toml")
